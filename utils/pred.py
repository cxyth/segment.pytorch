# -*- encoding: utf-8 -*-
'''
@Time       : 06/14/22 10:35 AM
@Author     : Jiang.xx
@Email      : cxyth@live.com
@Description:
'''

import os
import sys
import cv2
import numpy as np
import torch
from torch.utils.data import Dataset
from scipy import ndimage as ndi
from osgeo import osr, ogr, gdal


class RSImageSlideWindowManager(object):

    def __init__(self, in_raster, out_raster, out_bands, window_sz, overlap):
        super(RSImageSlideWindowManager, self).__init__()
        self.window_sz = window_sz
        self.overlap = overlap
        self.in_raster = gdal.Open(in_raster, gdal.GA_ReadOnly)
        self.img_w = self.in_raster.RasterXSize
        self.img_h = self.in_raster.RasterYSize
        self.out_bands = out_bands
        proj = self.in_raster.GetProjection()  # 获取投影信息
        geotrans = self.in_raster.GetGeoTransform()  # 仿射矩阵
        driver = gdal.GetDriverByName("GTiff")
        self.out_raster = driver.Create(out_raster, self.img_w, self.img_h, out_bands, gdal.GDT_Byte)
        self.out_raster.SetGeoTransform(geotrans)
        self.out_raster.SetProjection(proj)
        # create windows
        self.windows = []
        stride = window_sz - overlap
        n_h = int(np.ceil((self.img_h - window_sz) / stride)) + 1
        n_w = int(np.ceil((self.img_w - window_sz) / stride)) + 1
        for i in range(n_h):
            dh = min(i * stride, self.img_h - window_sz)
            for j in range(n_w):
                dw = min(j * stride, self.img_w - window_sz)
                self.windows.append([dh, dh + window_sz, dw, dw + window_sz])
        self.window_i = 0

    def __len__(self):
        return len(self.windows)

    def get_next(self):
        if self.window_i == len(self.windows):
            return None
        y1, y2, x1, x2 = self.windows[self.window_i]
        im_data = self.in_raster.ReadAsArray(xoff=x1, yoff=y1, xsize=self.window_sz, ysize=self.window_sz)
        assert im_data.max() <= 256, im_data.max()
        if im_data.ndim == 2:   # 二值图一般是二维，需要添加一个维度
            im_data = im_data[np.newaxis, :, :]
        self.window_i += 1
        return im_data.astype(np.uint8)

    def fit_result(self, result):
        index = self.window_i - 1
        y1, y2, x1, x2 = self.windows[index]
        dy1 = 0 if y1 == 0 else self.overlap // 2
        dy2 = 0 if y2 == self.img_h else self.overlap // 2
        dx1 = 0 if x1 == 0 else self.overlap // 2
        dx2 = 0 if x2 == self.img_w else self.overlap // 2
        for i in range(self.out_bands):
            self.out_raster.GetRasterBand(i + 1).\
                WriteArray(result[i, dy1:self.window_sz-dy2, dx1:self.window_sz-dx2], x1+dx1, y1+dy1)

    def close(self):
        self.in_raster = None
        self.out_raster = None


class ImageDataset(Dataset):

    def __init__(self, im_data, tile_size, overlap, transform, channel_first=False):
        if channel_first:
            im_data = np.transpose(im_data, (1, 2, 0))
        self.image = im_data
        self.tile_size = tile_size
        self.overlap = overlap
        self.transform = transform
        self.tile_coords = self._get_tile_coordinates(im_data)

    def _get_tile_coordinates(self, image):
        stride = self.tile_size - self.overlap
        img_h, img_w, img_c = image.shape
        n_h = int(np.ceil((img_h - self.tile_size) / stride)) + 1
        n_w = int(np.ceil((img_w - self.tile_size) / stride)) + 1
        windows = []
        for i in range(n_h):
            dh = min(i * stride, img_h - self.tile_size)
            for j in range(n_w):
                dw = min(j * stride, img_w - self.tile_size)
                if np.sum(image[dh:dh + self.tile_size, dw:dw + self.tile_size, :]) == 0:
                    continue
                windows.append([dh, dh + self.tile_size, dw, dw + self.tile_size])
        return windows

    def __len__(self):
        return len(self.tile_coords)

    def __getitem__(self, i):
        window = self.tile_coords[i]
        y1, y2, x1, x2 = window
        tile = self.image[y1:y2, x1:x2]
        transformed = self.transform(image=tile)
        tile = transformed['image']

        return {
            'image': tile,
            'window': np.array(window)
        }


class WeightedPredictManager(object):

    def __init__(self, map_height, map_width, map_channel, patch_height, patch_width):
        self.map_h = map_height
        self.map_w = map_width
        self.map_c = map_channel
        self.patch_h = patch_height
        self.patch_w = patch_width
        # (C, H, W)
        self.map = np.zeros((map_channel, map_height, map_width), dtype=np.float16)
        self.weight_map = np.zeros((1, map_height, map_width), dtype=np.float16)
        # Compute patch pixel weights to merge overlapping patches back together smoothly:
        patch_weights = np.ones((patch_height + 2, patch_width + 2), dtype=np.float16)
        patch_weights[0, :] = 0
        patch_weights[-1, :] = 0
        patch_weights[:, 0] = 0
        patch_weights[:, -1] = 0
        patch_weights = ndi.distance_transform_edt(patch_weights)
        self.patch_weights = patch_weights[None, 1:-1, 1:-1]

    def update(self, preds, windows):
        assert preds.ndim == 4, preds.shpae
        for i in range(preds.shape[0]):
            # 更新一个window区域的预测概率图
            pred = preds[i]
            y1, y2, x1, x2 = windows[i]
            assert y1 + self.patch_h <= self.map_h and x1 + self.patch_w <= self.map_w
            self.map[:, y1:y1 + self.patch_h, x1:x1 + self.patch_w] += self.patch_weights * pred
            self.weight_map[:, y1:y1 + self.patch_h, x1:x1 + self.patch_w] += self.patch_weights

    def get_result(self):
        probmap = self.map / self.weight_map
        mask = np.argmax(probmap, axis=0).astype(np.uint8)
        return mask, probmap

    def reset(self):
        self.map[...] = 0.
        self.weight_map[...] = 0.


class CenterClippingPredictManager(object):

    def __init__(self, map_height, map_width, map_channel, patch_height, patch_width):
        self.map_h = map_height
        self.map_w = map_width
        self.map_c = map_channel
        self.patch_h = patch_height
        self.patch_w = patch_width
        # (C, H, W)
        self.map = np.zeros((map_channel, map_height, map_width), dtype=np.float16)
        self.weight_map = np.zeros((1, map_height, map_width), dtype=np.float16)
        # Compute patch pixel weights to merge overlapping patches back together smoothly:
        patch_weights = np.ones((patch_height + 2, patch_width + 2), dtype=np.float16)
        patch_weights[0, :] = 0
        patch_weights[-1, :] = 0
        patch_weights[:, 0] = 0
        patch_weights[:, -1] = 0
        patch_weights = ndi.distance_transform_edt(patch_weights)
        self.patch_weights = patch_weights[None, 1:-1, 1:-1]

    def update(self, pred, yoff, xoff):
        # 更新一个patch区域的预测概率图
        assert yoff + self.patch_h <= self.map_h and xoff + self.patch_w <= self.map_w
        self.map[:, yoff:yoff + self.patch_h, xoff:xoff + self.patch_w] += self.patch_weights * pred
        self.weight_map[:, yoff:yoff + self.patch_h, xoff:xoff + self.patch_w] += self.patch_weights

    def get_result(self):
        return self.map / self.weight_map

    def reset(self):
        self.map[...] = 0.
        self.weight_map[...] = 0.

'''
class raster_Generater(object):
    def __init__(self, raster, tile_size, overlap, transform):
        self.raster = raster
        self.transform = transform
        self.idx = -1
        self.windows = []
        img_w = raster.RasterXSize
        img_h = raster.RasterYSize
        stride = tile_size - overlap
        n_h = int(np.ceil((img_h - tile_size) / stride)) + 1
        n_w = int(np.ceil((img_w - tile_size) / stride)) + 1
        for i in range(n_h):
            dh = min(i * stride, img_h - tile_size)
            for j in range(n_w):
                dw = min(j * stride, img_w - tile_size)
                self.windows.append([dh, dh+tile_size, dw, dw+tile_size])
        self.len = len(self.windows)

    def __len__(self):
        return self.len

    def __iter__(self):
        return self

    def __next__(self):
        self.idx += 1
        if self.idx < self.len:
            window = self.windows[self.idx]
            y1, y2, x1, x2 = window
            patch = self.raster.ReadAsArray(x1, y1, x2-x1, y2-y1)
            patch = np.transpose(patch, (1, 2, 0))
            assert patch.max() <= 255, patch.max()
            patch = patch.astype(np.uint8)
            transformed = self.transform(image=patch)
            patch = transformed['image'].unsqueeze(0)

            return {
                'image': patch,
                'window': window
            }
        else:
            raise StopIteration()
'''


if __name__ == '__main__':
    from tqdm import tqdm
    pass
    in_path = '/home/Jiang/workspace/water/foshan/foshan.tif'
    out_path = './test.tif'
    out_bands = 3
    window_sz = 2560
    overlap = 256

    G = GDAL_GEN(in_path, out_path, out_bands, window_sz, overlap)
    for i in tqdm(range(len(G))):
        img = G.get_next()
        print(img.shape)
        G.fit_result(img)



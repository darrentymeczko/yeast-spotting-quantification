"""ImageJ-compatible sliding-paraboloid subtraction, implemented in Python.

Algorithm adapted from Michael Schmid's public-domain ImageJ
ij/plugin/filter/BackgroundSubtracter.java (1.54p). Reference:
https://github.com/imagej/ImageJ/blob/v1.54p/ij/plugin/filter/BackgroundSubtracter.java
The directional pass order, maximum-filter offset, corner correction and
float32 arithmetic are intentional. A separable morphological opening is not
an interchangeable implementation of this measurement protocol.

Numba compiles the Python loops locally; neither Java nor FIJI is used.
"""
from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True)
def _line(pixels, start, inc, length, c, cache, next_point, edges):
    minimum = np.float32(np.inf)
    last = 0
    first_corner, last_corner = length - 1, 0
    prev1 = prev2 = np.float32(0)
    curvature = np.float32(1.999) * c
    for i in range(length):
        v = pixels[start + i * inc]
        cache[i] = v
        minimum = min(minimum, v)
        if i >= 2 and prev1 + prev1 - prev2 - v < curvature:
            next_point[last] = i - 1
            last = i - 1
        prev2, prev1 = prev1, v
    next_point[last] = length - 1
    next_point[length - 1] = 2147483647
    i1 = 0
    while i1 < length - 1:
        v1 = cache[i1]
        slope_min = np.float32(np.inf)
        i2, search_to, recalc = 0, length, 0
        j = next_point[i1]
        while j < search_to:
            distance = np.float32(j - i1)
            slope = (cache[j] - v1) / distance + c * distance
            if slope < slope_min:
                slope_min, i2, recalc = slope, j, -3
            if recalc == 0:
                b = float(np.float32(0.5) * slope_min / c)
                limit = i1 + int(b + np.sqrt(b*b + float((v1-minimum)/c)) + 1)
                if 0 < limit < search_to:
                    search_to = limit
            j = next_point[j]
            recalc += 1
        if i1 == 0:
            first_corner = i2
        if i2 == length - 1:
            last_corner = i1
        for j in range(i1 + 1, i2):
            d = np.float32(j - i1)
            pixels[start + j*inc] = v1 + d * (slope_min - d*c)
        i1 = i2
    if not edges:
        return np.float32(0), np.float32(0)
    if 4 * first_corner >= length:
        first_corner = 0
    if 4 * (length - 1 - last_corner) >= length:
        last_corner = length - 1
    span = np.float32(last_corner - first_corner)
    slope = (cache[last_corner] - cache[first_corner]) / span
    value0 = cache[first_corner] - slope * np.float32(first_corner)
    coeff6 = np.float32(0)
    mid = np.float32(0.5) * np.float32(last_corner + first_corner)
    for i in range((length+2)//3, (2*length)//3 + 1):
        dx = (np.float32(i)-mid)*np.float32(2)/span
        poly6 = dx*dx*dx*dx*dx*dx - np.float32(1)
        if cache[i] < value0 + slope*np.float32(i) + coeff6*poly6:
            coeff6 = -(value0 + slope*np.float32(i) - cache[i])/poly6
    dx = (np.float32(first_corner)-mid)*np.float32(2)/span
    left = (value0 + coeff6*(dx*dx*dx*dx*dx*dx-np.float32(1))
            + c*np.float32(first_corner)*np.float32(first_corner))
    dx = (np.float32(last_corner)-mid)*np.float32(2)/span
    d = np.float32(length-1-last_corner)
    right = (value0 + np.float32(length-1)*slope
             + coeff6*(dx*dx*dx*dx*dx*dx-np.float32(1)) + c*d*d)
    return left, right


@njit(cache=True)
def _filter3(p, w, h, maximum):
    shift = 0.0
    for n, count, step, line_step in ((w, h, 1, w), (h, w, w, 1)):
        for line in range(count):
            start = line * line_step
            v3 = p[start]
            v2 = v3
            for i in range(n):
                at = start + i*step
                v1, v2 = v2, v3
                if i < n-1:
                    v3 = p[at+step]
                if maximum:
                    value = max(v1, v2, v3)
                    shift += float(value-v2)
                    p[at] = value
                else:
                    p[at] = (v1+v2+v3)*np.float32(0.33333333)
    return np.float32(shift/w/h)


@njit(cache=True)
def _background(p, w, h, radius, presmooth, correct_corners):
    c = np.float32(0.5)/radius
    diagonal = np.float32(1)/radius
    cache = np.empty(max(w, h), dtype=np.float32)
    next_point = np.empty(max(w, h), dtype=np.int64)
    shift = np.float32(0)
    if presmooth:
        shift = _filter3(p, w, h, True)
        _filter3(p, w, h, False)
    if correct_corners:
        corners = np.zeros(4, dtype=np.float32)
        a, b = _line(p, 0, 1, w, c, cache, next_point, True)
        corners[0], corners[1] = a, b
        a, b = _line(p, (h-1)*w, 1, w, c, cache, next_point, True)
        corners[2], corners[3] = a, b
        a, b = _line(p, 0, w, h, c, cache, next_point, True)
        corners[0] += a
        corners[2] += b
        a, b = _line(p, w-1, w, h, c, cache, next_point, True)
        corners[1] += a
        corners[3] += b
        for k, start, inc in ((0, 0, w+1), (1, w-1, w-1),
                              (2, (h-1)*w, 1-w), (3, w*h-1, -1-w)):
            a, b = _line(p, start, inc, min(w, h), diagonal, cache, next_point, True)
            corners[k] += a
        for k, at in enumerate((0, w-1, (h-1)*w, w*h-1)):
            p[at] = min(p[at], corners[k]/np.float32(3))
    for direction in (0, 1, 0, 2, 3, 4, 5, 2, 3):
        if direction == 0:
            first, count, line_step, step, length = 0, h, w, 1, w
        elif direction == 1:
            first, count, line_step, step, length = 0, w, 1, w, h
        elif direction == 2:
            first, count, line_step, step, length = 0, w-2, 1, w+1, 0
        elif direction == 3:
            first, count, line_step, step, length = 1, h-2, w, w+1, 0
        elif direction == 4:
            first, count, line_step, step, length = 2, w, 1, w-1, 0
        else:
            first, count, line_step, step, length = 0, h-2, w, w-1, 0
        for i in range(first, count):
            start = i*line_step
            if direction == 2:
                length = min(h, w-i)
            elif direction == 3:
                length = min(w, h-i)
            elif direction == 4:
                length = min(h, i+1)
            elif direction == 5:
                start += w-1
                length = min(w, h-i)
            _line(p, start, step, length, c if direction < 2 else diagonal,
                  cache, next_point, False)
    p -= shift
    return p


def subtract_background(image, radius, *, presmooth=True, correct_corners=True):
    """Return unclipped float32 subtraction, matching ImageJ's 32-bit path.

    Input is a finite 2-D grayscale image. Radius is rounded to whole pixels
    as the project's former FIJI macro did. The input is never modified.
    """
    src = np.asarray(image, dtype=np.float32)
    if src.ndim != 2 or min(src.shape) < 3 or not np.isfinite(src).all():
        raise ValueError("background subtraction needs a finite 2-D image at least 3x3")
    if not np.isfinite(radius) or radius <= 0:
        raise ValueError("background radius must be finite and positive")
    radius = np.float32(round(max(1.0, float(radius))))
    h, w = src.shape
    bg = _background(src.ravel().copy(), w, h, radius, presmooth, correct_corners)
    return src - bg.reshape(src.shape)

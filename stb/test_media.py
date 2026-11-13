#!/usr/bin/env python3
"""Uji helper mode-media di display.py — jalan di STB (butuh gi).

    python3 /root/test_media.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from display import fit_box, media_file_ext   # noqa: E402


class FitBox(unittest.TestCase):
    """(tw, th, left, right, top, bottom) — skala fit ke 1920x1080
    preserve aspect + border tengah."""

    def test_16_9_penuh_tanpa_border(self):
        self.assertEqual(fit_box(1280, 720), (1920, 1080, 0, 0, 0, 0))

    def test_4_3_pillarbox(self):
        tw, th, l, r, t, b = fit_box(640, 480)
        self.assertEqual(th, 1080)
        self.assertEqual(tw, 1440)
        self.assertEqual(l, 240)
        self.assertEqual(r, 240)
        self.assertEqual((t, b), (0, 0))

    def test_portrait_9_16_pillarbox_tinggi(self):
        tw, th, l, r, t, b = fit_box(1080, 1920)
        self.assertEqual(th, 1080)
        self.assertEqual(tw, 606)          # 1920*1080/1920 = 607.5 -> 606 even
        self.assertEqual(l + tw + r, 1920)
        self.assertEqual(t, 0)

    def test_lebih_besar_dari_frame_diskala_turun(self):
        tw, th, l, r, t, b = fit_box(3840, 2160)
        self.assertEqual((tw, th), (1920, 1080))

    def test_aneh_aspect_fit_dalam_frame(self):
        tw, th, l, r, t, b = fit_box(700, 500)
        self.assertLessEqual(tw, 1920)
        self.assertLessEqual(th, 1080)
        self.assertEqual(l + tw + r, 1920)
        self.assertEqual(t + th + b, 1080)
        self.assertEqual(tw % 2, 0)
        self.assertEqual(th % 2, 0)

    def test_dimensi_nol_kembali_fullframe(self):
        self.assertEqual(fit_box(0, 0), (1920, 1080, 0, 0, 0, 0))


class MediaFileExt(unittest.TestCase):
    def test_video_dikenal(self):
        for ext in (".mp4", ".mkv", ".mov", ".ts", ".webm",
                    ".hevc", ".h264", ".h265"):
            self.assertTrue(media_file_ext("a" + ext), ext)
            self.assertTrue(media_file_ext("a" + ext.upper()), ext)

    def test_foto_dikenal(self):
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            self.assertTrue(media_file_ext("a" + ext), ext)
            self.assertTrue(media_file_ext("a" + ext.upper()), ext)

    def test_nonvideo_ditolak(self):
        for f in ("a.exe", "a.txt", "a", "a.mp4.tmp"):
            self.assertFalse(media_file_ext(f), f)


if __name__ == "__main__":
    unittest.main(verbosity=2)

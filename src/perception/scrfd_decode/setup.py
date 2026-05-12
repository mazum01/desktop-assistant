"""
Build script for the scrfd_decode_cpp pybind11 extension.

Usage (from repo root or this directory):
    python3 src/perception/scrfd_decode/setup.py build_ext --inplace

The resulting .so lands in src/perception/scrfd_decode/ and is imported
lazily by FaceDetector with a pure-Python fallback if absent.
"""

from setuptools import setup, Extension
import pybind11

ext = Extension(
    name="scrfd_decode_cpp",
    sources=["scrfd_decode.cpp"],
    include_dirs=[pybind11.get_include()],
    extra_compile_args=[
        "-O3",
        "-march=native",       # Cortex-A76 / Pi 5 aarch64
        "-ffast-math",
        "-std=c++17",
        "-fvisibility=hidden",
    ],
    language="c++",
)

setup(
    name="scrfd_decode_cpp",
    version="1.0",
    ext_modules=[ext],
)

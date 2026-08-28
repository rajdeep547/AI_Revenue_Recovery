"""Offline evaluation harness. NOT part of the pipeline.

Modules here may read ``ground_truth.json``. Nothing under ``app/`` may import
from this package — that separation is asserted by the datagen test suite.
"""

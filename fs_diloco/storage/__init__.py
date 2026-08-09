"""Persistence adapters.

The package initializer stays intentionally lightweight: learner admission
imports filesystem helpers before it is allowed to import torch/safetensors.
Callers import concrete adapters from their defining modules.
"""

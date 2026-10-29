"""Image/text embedders for visual page search. All run locally on the Mac's GPU (PyTorch MPS); nothing leaves the machine.

Each embedder maps page images and text queries into one vector space (cosine similarity, L2-normalised).
Candidates (all Apache-2.0): EmbeddingGemma 2 (Google, 768-d), LlamaIndex VDR 2B (Qwen2-VL base, 1536-d),
Qwen3-VL-Embedding-2B (2048-d). A bake-off on 21 real files picked gemma (VISION_MODEL); the other two stay selectable.
"""
from __future__ import annotations

import numpy as np

QUERY_INSTRUCTION = "Find the document page, drawing, chart or photo that answers this question."
MODELS = {
    "gemma": {"hf": "google/embeddinggemma-2", "dims": 768},
    "vdr": {"hf": "llamaindex/vdr-2b-multi-v1", "dims": 1536},
    "qwen": {"hf": "Qwen/Qwen3-VL-Embedding-2B", "dims": 2048},
}


class Embedder:
    def __init__(self, name: str):
        import torch
        from sentence_transformers import SentenceTransformer
        if name not in MODELS:
            raise ValueError(f"unknown model {name}; choose from {list(MODELS)}")
        self.name, self.dims, hf = name, MODELS[name]["dims"], MODELS[name]["hf"]
        kw: dict = {"device": "mps"}
        if name == "qwen":
            kw["model_kwargs"] = {"dtype": torch.bfloat16}
        if name == "vdr":
            kw.update(trust_remote_code=True, model_kwargs={"torch_dtype": torch.bfloat16, "attn_implementation": "sdpa"})
        self.model = SentenceTransformer(hf, **kw)
        self.batch = 8 if name == "gemma" else 2

    def images(self, imgs: list) -> np.ndarray:
        return np.asarray(self.model.encode(imgs, batch_size=self.batch, normalize_embeddings=True), dtype=np.float32)

    def queries(self, texts: list[str]) -> np.ndarray:
        if self.name == "gemma":
            v = self.model.encode(texts, prompt_name="SearchQuery", normalize_embeddings=True)
        elif self.name == "qwen":
            v = self.model.encode(texts, prompt=QUERY_INSTRUCTION, normalize_embeddings=True)
        else:
            v = self.model.encode(texts, normalize_embeddings=True)
        return np.asarray(v, dtype=np.float32)

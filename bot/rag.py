"""
Oddiy RAG: data/docs/*.md fayllarni bo'laklarga bo'lib, Gemini embedding API
orqali vektorlashtiradi va xotirada (in-memory) saqlaydi. Savol kelganda
savolni ham embed qilib, cosine similarity bo'yicha top-k bo'lakni topadi.

Eslatma: bu prototip darajasidagi RAG - kichik hujjat to'plami (5-10 fayl) uchun
in-memory saqlash yetarli. Hujjatlar ko'payib ketsa pgvector/ChromaDB'ga o'tish kerak.
"""
import glob
import os

from dotenv import load_dotenv

load_dotenv()  # bu modul boshqa joydan import qilinganda ham .env kafolatli yuklansin

import numpy as np

try:
    import google.generativeai as genai

    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    _HAS_GEMINI = bool(os.getenv("GEMINI_API_KEY"))
except Exception:
    _HAS_GEMINI = False

EMBED_MODEL = "models/gemini-embedding-001"
CHUNK_SIZE = 500  # belgi (character) bo'yicha taxminiy bo'lak hajmi


class DocStore:
    def __init__(self, docs_dir: str = "data/docs"):
        self.docs_dir = docs_dir
        self.chunks: list[str] = []
        self.sources: list[str] = []
        self.vectors: np.ndarray | None = None
        self._vocab: dict[str, int] = {}  # faqat fallback rejimida ishlatiladi

    def _chunk_text(self, text: str) -> list[str]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks, buf = [], ""
        for p in paragraphs:
            if len(buf) + len(p) < CHUNK_SIZE:
                buf += ("\n\n" if buf else "") + p
            else:
                if buf:
                    chunks.append(buf)
                buf = p
        if buf:
            chunks.append(buf)
        return chunks

    def _embed(self, texts: list[str], build_vocab: bool = False) -> np.ndarray:
        if _HAS_GEMINI:
            try:
                vecs = []
                for t in texts:
                    r = genai.embed_content(model=EMBED_MODEL, content=t)
                    vecs.append(r["embedding"])
                return np.array(vecs, dtype=np.float32)
            except Exception as e:
                print(f"[rag] Gemini embedding xato, fallback ishlatilmoqda: {e}")
        # Fallback: oddiy bag-of-words vektor (API kaliti bo'lmasa ham ishlaydi).
        # Lug'at faqat build() vaqtida (build_vocab=True) kengaytiriladi, shunda
        # keyingi qidiruv so'rovlari doim bir xil o'lchamdagi vektorga ega bo'ladi.
        if build_vocab:
            for t in texts:
                for w in t.lower().split():
                    self._vocab.setdefault(w, len(self._vocab))
        dim = max(len(self._vocab), 1)
        mat = np.zeros((len(texts), dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                j = self._vocab.get(w)
                if j is not None:
                    mat[i, j] += 1
        return mat

    def build(self):
        self.chunks, self.sources = [], []
        for path in sorted(glob.glob(os.path.join(self.docs_dir, "*.md"))):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for c in self._chunk_text(text):
                self.chunks.append(c)
                self.sources.append(os.path.basename(path))
        self._vocab = {}
        if self.chunks:
            self.vectors = self._embed(self.chunks, build_vocab=True)
        print(f"[rag] {len(self.chunks)} ta bo'lak indekslandi ({self.docs_dir})")

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        if not self.chunks or self.vectors is None:
            return []
        q_vec = self._embed([query], build_vocab=False)
        sims = self.vectors @ q_vec[0] / (
            np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(q_vec[0]) + 1e-8
        )
        top_idx = np.argsort(-sims)[:top_k]
        return [
            {"text": self.chunks[i], "source": self.sources[i], "score": float(sims[i])}
            for i in top_idx
            if sims[i] > 0.05
        ]


# Modul yuklanganda bitta global instance tayyorlanadi
store = DocStore()

# core/retrieve_funnel.py
"""三层检索：路由 → BM25 与向量召回 → 可选的短名单重打分。

热路径不用 LLM 重排。语料从 Chroma 读出后在进程内做 BM25。
向量模型只用 rag_core 里的那一个。
"""
import math
import re
from collections import Counter

import numpy as np

from config import CHROMA_PATH

_RRF_K = 60
_RECALL_N = 20
# 题集上重打分的 Recall@5 没有高于纯向量，热路径不跑第 2 层。
RERANK = False

_PATH_RE = re.compile(r"/Game/[A-Za-z0-9_./]+")
_ERROR_RE = re.compile(r"error\s*C\d+", re.I)
_CLASS_RE = re.compile(r"\bA[A-Z][A-Za-z0-9_]*\b")
_BP_RE = re.compile(r"\bBP_[A-Za-z0-9_]+\b")
_FILE_RE = re.compile(r"\b[A-Za-z0-9_]+\.(?:h|cpp)\b")
_TOKEN_RE = re.compile(r"[a-z0-9_./:+-]+|[\u4e00-\u9fff]", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_DOMAIN_SKILLS = {
    "blueprint": ("ue-blueprint-skill", "ue-verify-skill"),
    "niagara": ("ue-niagara-skill",),
    "material": ("ue-material-skill",),
    "umg": ("ue-umg-skill",),
    "pcg": ("ue-pcg-skill",),
    "sequencer": ("ue-sequencer-skill",),
    "animation": ("ue-animation-skill",),
    "static-mesh": ("ue-static-mesh-skill",),
    "actor": ("ue-actor-skill", "ue-verify-skill"),
    "cpp": ("ue-cpp-plan-skill", "ue-debug-skill"),
    "asset": ("ue-asset-skill",),
    "history": ("ue-history-skill",),
}

_CACHE: dict[str, dict] = {}


def _tokenize(text: str) -> list[str]:
    text = text or ""
    tokens = [tok.lower() for tok in _TOKEN_RE.findall(text)]
    for seg in _CJK_RE.findall(text):
        tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        self.df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        self.dl: list[int] = []
        for doc in docs:
            counts = Counter(doc)
            self.tf.append(dict(counts))
            self.dl.append(len(doc))
            for term in counts:
                self.df[term] = self.df.get(term, 0) + 1
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0

    def top(self, query: str, indexes: list[int], n: int) -> list[int]:
        if not indexes or n <= 0:
            return []
        scores = {i: 0.0 for i in indexes}
        for term in set(_tokenize(query)):
            df = self.df.get(term, 0)
            if df == 0 or self.n == 0:
                continue
            idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
            for i in indexes:
                freq = self.tf[i].get(term, 0)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * self.dl[i] / (self.avgdl or 1.0))
                scores[i] += idf * (freq * (self.k1 + 1.0)) / denom
        ranked = sorted(indexes, key=lambda i: scores[i], reverse=True)
        return [i for i in ranked if scores[i] > 0][:n]


def _corpus(collection: str) -> dict:
    cached = _CACHE.get(collection)
    if cached is not None:
        return cached
    import chromadb

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    col = client.get_collection(collection)
    got = col.get(include=["documents", "metadatas"])
    ids = list(got.get("ids") or [])
    docs = list(got.get("documents") or [])
    metas = list(got.get("metadatas") or [])
    if len(docs) < len(ids):
        docs.extend([""] * (len(ids) - len(docs)))
    if len(metas) < len(ids):
        metas.extend([{}] * (len(ids) - len(metas)))
    cached = {
        "col": col,
        "ids": ids,
        "docs": docs,
        "metas": [meta or {} for meta in metas],
        "bm25": BM25([_tokenize(doc or "") for doc in docs]),
        "index": {ids[i]: i for i in range(len(ids))},
    }
    _CACHE[collection] = cached
    return cached


def _match_where(meta: dict, where: dict | None) -> bool:
    if not where:
        return True
    for key, value in where.items():
        if str(meta.get(key, "")) != str(value):
            return False
    return True


def _nouns(query: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for kind, pattern in (
        ("path", _PATH_RE),
        ("error", _ERROR_RE),
        ("class", _CLASS_RE),
        ("bp", _BP_RE),
        ("file", _FILE_RE),
    ):
        for token in pattern.findall(query or ""):
            found.append((kind, token))
    return found


def _match_noun(rec_meta: dict, doc: str, kind: str, token: str) -> bool:
    src = str(rec_meta.get("source_path") or "")
    if kind == "path":
        package = str(rec_meta.get("package_name") or "")
        object_path = str(rec_meta.get("object_path") or "")
        name = token.rsplit("/", 1)[-1]
        dotted = f"{token}.{name}" if name else token
        return src == token or package == token or object_path in (token, dotted)
    if kind == "class":
        return str(rec_meta.get("class_name") or "") == token
    if kind == "bp":
        return str(rec_meta.get("asset_name") or "") == token or str(rec_meta.get("actor_label") or "") == token
    if kind == "file":
        file_name = str(rec_meta.get("file_name") or "")
        return file_name == token or src.endswith("\\" + token) or src.endswith("/" + token)
    if kind == "error":
        if str(rec_meta.get("source_type") or "") != "ue_error":
            return False
        return token.lower().replace(" ", "") in (doc or "").lower().replace(" ", "")
    return False


def _route_index(query: str, corpus: dict, indexes: list[int]) -> int | None:
    groups: list[set[int]] = []
    for kind, token in _nouns(query):
        hits = {
            i for i in indexes
            if _match_noun(corpus["metas"][i], corpus["docs"][i], kind, token)
        }
        if hits:
            groups.append(hits)
    if not groups:
        return None
    inter = set.intersection(*groups)
    if len(inter) == 1:
        return next(iter(inter))
    return None


def route_domain(query: str) -> str | None:
    """给技能卡片用。失败现象先分到历史解决方案，对不上再由该技能转去域技能。"""
    text = (query or "").lower()
    history_keys = (
        "写不成任务", "历史解决方案", "不生效", "查不到", "没反应",
        "报错", "失败", "kind=none", "对不上", "不猜",
    )
    if any(key in text for key in history_keys):
        return "history"
    nouns = _nouns(query or "")
    kinds = {kind for kind, _token in nouns}
    if "bp" in kinds or "path" in kinds:
        return "blueprint"
    if "class" in kinds or "file" in kinds or "error" in kinds:
        return "cpp"
    text = (query or "").lower()
    rules = (
        ("blueprint", ("蓝图", "blueprint")),
        ("niagara", ("niagara", "粒子")),
        ("material", ("材质", "material")),
        ("umg", ("umg", "widget", "控件")),
        ("pcg", ("pcg",)),
        ("sequencer", ("sequencer", "过场")),
        ("animation", ("动画", "骨骼", "control rig", "skeletal")),
        ("static-mesh", ("静态网格", "nanite", "static mesh")),
        ("actor", ("actor", "关卡", "移动", "坐标")),
        ("cpp", ("c++", "编译错误", "error c")),
        ("asset", ("资产", "资源")),
    )
    for domain, keys in rules:
        if any(key in text for key in keys):
            return domain
    return None


def skill_names_for(query: str) -> tuple[str, ...] | None:
    domain = route_domain(query)
    if not domain:
        return None
    return _DOMAIN_SKILLS.get(domain)


def _title_overlaps(query: str, title: str) -> bool:
    """标题里的词出现在现象原文里才采用这一节，避免把唯一的一条硬塞进上下文。"""
    for token in re.findall(r"[\u4e00-\u9fff]{4,}", title or ""):
        if token in (query or ""):
            return True
    for token in re.findall(r"[A-Za-z0-9_]{4,}", title or ""):
        if token.lower() in (query or "").lower():
            return True
    return False


def matching_solution(query: str) -> str:
    """历史方案和排障条目各取 top 1。标题对上才返回那一节。"""
    from core.rag_core import retrieve

    for kind in ("history", "debug"):
        hits = retrieve(query, top_k=1, where={"doc_kind": kind})
        if not hits:
            continue
        title = str((hits[0].get("metadata") or {}).get("title") or "")
        if not _title_overlaps(query, title):
            continue
        return (hits[0].get("content") or "")[:1200]
    return ""


def _dense_ids(corpus: dict, query: str, where: dict | None, n: int) -> list[str]:
    n = min(n, len(corpus["ids"]))
    if n <= 0:
        return []
    from core.rag_core import _get_model

    model = _get_model()
    emb = model.encode([query], normalize_embeddings=True).tolist()
    kwargs = {"query_embeddings": emb, "n_results": n}
    if where:
        kwargs["where"] = where
    try:
        res = corpus["col"].query(**kwargs)
    except Exception:
        kwargs["n_results"] = 1
        try:
            res = corpus["col"].query(**kwargs)
        except Exception:
            return []
    return list((res.get("ids") or [[]])[0])


def _embeddings(corpus: dict, ids: list[str]) -> dict[str, list[float]]:
    if not ids:
        return {}
    got = corpus["col"].get(ids=ids, include=["embeddings"])
    out = {}
    vectors = got.get("embeddings") or []
    for i, item_id in enumerate(got.get("ids") or []):
        if i < len(vectors) and vectors[i] is not None:
            out[item_id] = vectors[i]
    return out


def _rrf(lists: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(scores, key=lambda item_id: scores[item_id], reverse=True)


def _row(corpus: dict, index: int, distance: float) -> dict:
    return {
        "id": corpus["ids"][index],
        "content": corpus["docs"][index] or "",
        "metadata": corpus["metas"][index] or {},
        "distance": float(distance),
    }


def retrieve_bm25(
    query: str,
    top_k: int = 5,
    where: dict | None = None,
    collection: str = "ue_assets",
) -> list[dict]:
    """评测用的纯 BM25。不走专有名词路由，也不走向量。热路径仍是 retrieve_funnel。"""
    corpus = _corpus(collection)
    if not corpus["ids"]:
        return []
    indexes = [i for i in range(len(corpus["ids"])) if _match_where(corpus["metas"][i], where)]
    if not indexes:
        return []
    ranked = corpus["bm25"].top(query, indexes, max(1, int(top_k)))
    return [_row(corpus, index, 1.0) for index in ranked]


def retrieve_funnel(
    query: str,
    top_k: int = 5,
    where: dict | None = None,
    collection: str = "ue_assets",
    rerank: bool | None = None,
) -> list[dict]:
    corpus = _corpus(collection)
    if not corpus["ids"]:
        return []
    indexes = [i for i in range(len(corpus["ids"])) if _match_where(corpus["metas"][i], where)]
    if not indexes:
        return []
    top_k = max(1, int(top_k))
    routed = _route_index(query, corpus, indexes)
    if routed is not None:
        return [_row(corpus, routed, 0.0)]

    recall_n = min(_RECALL_N, len(indexes))
    bm25_ids = [corpus["ids"][i] for i in corpus["bm25"].top(query, indexes, recall_n)]
    dense_ids = [item_id for item_id in _dense_ids(corpus, query, where, recall_n) if item_id in corpus["index"]]
    merged = _rrf([bm25_ids, dense_ids])[:_RECALL_N]
    if not merged:
        return []

    use_rerank = RERANK if rerank is None else rerank
    order = merged
    distance_of: dict[str, float] = {}
    if use_rerank:
        try:
            from core.rag_core import _get_model

            query_vec = np.asarray(_get_model().encode([query], normalize_embeddings=True)[0])
            vectors = _embeddings(corpus, merged)
            scored = []
            for item_id in merged:
                vector = vectors.get(item_id)
                if vector is None:
                    scored.append((0.0, item_id))
                    continue
                cosine = float(np.dot(query_vec, np.asarray(vector)))
                scored.append((cosine, item_id))
            scored.sort(key=lambda item: item[0], reverse=True)
            order = [item_id for _score, item_id in scored]
            distance_of = {item_id: 1.0 - score for score, item_id in scored}
        except Exception:
            order = merged

    rows = []
    for item_id in order[:top_k]:
        rows.append(_row(corpus, corpus["index"][item_id], distance_of.get(item_id, 1.0)))
    return rows

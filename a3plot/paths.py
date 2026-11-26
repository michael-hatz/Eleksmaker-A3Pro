"""Polylinien vereinfachen und die Zeichenreihenfolge optimieren."""
from __future__ import annotations

import numpy as np


def simplify(path: np.ndarray, tol: float) -> np.ndarray:
    """Ramer-Douglas-Peucker. Behält Anfangs- und Endpunkt."""
    n = len(path)
    if n <= 2 or tol <= 0:
        return path
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = path[i], path[j]
        seg = path[i + 1:j]
        ab = b - a
        L2 = ab @ ab
        if L2 == 0:
            d = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            t = np.clip(((seg - a) @ ab) / L2, 0, 1)
            proj = a + t[:, None] * ab
            d = np.hypot(seg[:, 0] - proj[:, 0], seg[:, 1] - proj[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return path[keep]


def path_length(p: np.ndarray) -> float:
    if len(p) < 2:
        return 0.0
    d = np.diff(p, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def order_paths(paths: list[np.ndarray], start=(0.0, 0.0)) -> list[np.ndarray]:
    """Gieriger Nächster-Nachbar mit Umdrehen der Pfade.

    Die Reihenfolge ist invariant gegenüber Verschieben/Skalieren/Drehen und
    wird deshalb nur einmal beim Laden berechnet.
    """
    n = len(paths)
    if n <= 1:
        return list(paths)
    try:
        from scipy.spatial import cKDTree
    except ImportError:   # pragma: no cover
        return _order_bruteforce(paths, start)
    if n <= 200:
        return _order_bruteforce(paths, start)

    starts = np.array([p[0] for p in paths])
    ends = np.array([p[-1] for p in paths])
    used = np.zeros(n, dtype=bool)
    out = []
    pos = np.asarray(start, dtype=float)

    def build():
        rest = np.flatnonzero(~used)
        pts = np.vstack([starts[rest], ends[rest]])
        ids = np.concatenate([rest, rest])
        rev = np.concatenate([np.zeros(len(rest), bool), np.ones(len(rest), bool)])
        return cKDTree(pts), ids, rev

    tree, ids, revs = build()
    while len(out) < n:
        choice = None
        k = 16
        while choice is None:
            k = min(k, len(ids))
            _, idx = tree.query(pos, k=k)
            for j in np.atleast_1d(idx):
                if not used[ids[j]]:
                    choice = (int(ids[j]), bool(revs[j]))
                    break
            if choice is None:
                if k >= 64 or k >= len(ids):
                    tree, ids, revs = build()   # viele verbrauchte Punkte: Baum neu bauen
                    k = 16
                else:
                    k *= 4
        pi, rev = choice
        used[pi] = True
        p = paths[pi][::-1] if rev else paths[pi]
        out.append(p)
        pos = p[-1]
    return out


def _order_bruteforce(paths, pos):
    n = len(paths)
    starts = np.array([p[0] for p in paths])
    ends = np.array([p[-1] for p in paths])
    used = np.zeros(n, dtype=bool)
    out = []
    pos = np.asarray(pos, dtype=float)
    for _ in range(n):
        ds = np.hypot(starts[:, 0] - pos[0], starts[:, 1] - pos[1])
        de = np.hypot(ends[:, 0] - pos[0], ends[:, 1] - pos[1])
        ds[used] = np.inf
        de[used] = np.inf
        i_s, i_e = int(np.argmin(ds)), int(np.argmin(de))
        if ds[i_s] <= de[i_e]:
            pi, rev = i_s, False
        else:
            pi, rev = i_e, True
        used[pi] = True
        p = paths[pi][::-1] if rev else paths[pi]
        out.append(p)
        pos = p[-1]
    return out


def join_close(paths: list[np.ndarray], tol: float) -> list[np.ndarray]:
    """Aufeinanderfolgende Pfade verbinden, wenn das Ende des einen fast der Anfang des nächsten ist."""
    out = []
    for p in paths:
        if out:
            last = out[-1]
            if np.hypot(*(last[-1] - p[0])) <= tol:
                out[-1] = np.vstack([last, p[1:]]) if len(p) > 1 else last
                continue
        out.append(p)
    return out

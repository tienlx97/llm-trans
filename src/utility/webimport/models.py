"""Site-independent shapes every adapter returns."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EpisodeRef:
    episode_id: str
    url: str
    title: str
    index: int                      # 1-based reading order across the work
    group_title: str = ""           # the site's chapter/arc heading, if any
    updated_hint: str = ""          # site-reported publish/revision stamp


@dataclass
class WorkInfo:
    site: str
    work_id: str
    url: str
    title: str
    author: str
    synopsis: str = ""
    episodes: List[EpisodeRef] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Episode:
    ref: EpisodeRef
    title: str
    body: List[str]
    preface: List[str] = field(default_factory=list)
    afterword: List[str] = field(default_factory=list)

    def content_hash(self) -> str:
        payload = "\x1e".join(["\x1f".join(self.preface), self.title, "\x1f".join(self.body), "\x1f".join(self.afterword)])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.ref.episode_id,
            "url": self.ref.url,
            "index": self.ref.index,
            "group_title": self.ref.group_title,
            "title": self.title,
            "preface": self.preface,
            "body": self.body,
            "afterword": self.afterword,
            "hash": self.content_hash(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Episode":
        ref = EpisodeRef(
            episode_id=str(data["episode_id"]), url=str(data.get("url") or ""), title=str(data.get("title") or ""),
            index=int(data.get("index") or 0), group_title=str(data.get("group_title") or ""),
        )
        return cls(ref=ref, title=str(data.get("title") or ""), body=list(data.get("body") or []),
                   preface=list(data.get("preface") or []), afterword=list(data.get("afterword") or []))


class AdapterError(RuntimeError):
    """The page did not have the structure the adapter expects."""


def optional_text(value: Optional[str]) -> str:
    return str(value or "").strip()

"""浏览器历史读取（Chrome / Edge / Chromium）。

实现要点：
- 不直接打开被浏览器占用的 History 数据库，而是先复制到临时目录再查询；
- 只读取最近 N 天的访问记录，且进程内去重，避免反复写入同一批浏览记录；
- 该能力默认关闭，需在设置中打开 browser_history_enabled。
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


@dataclass
class BrowserVisit:
    title: str = ""
    url: str = ""
    domain: str = ""
    visit_time: dt.datetime | None = None
    visit_count: int = 0


def _profile_dirs() -> list[Path]:
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    home = Path.home()

    if local:
        candidates += [
            Path(local) / "Google/Chrome/User Data",
            Path(local) / "Microsoft/Edge/User Data",
            Path(local) / "Chromium/User Data",
            Path(local) / "BraveSoftware/Brave-Browser/User Data",
        ]
    if appdata:
        candidates += [Path(appdata) / "Google/Chrome/User Data"]
    if home:
        candidates += [
            home / ".config/google-chrome",
            home / ".config/chromium",
            home / ".config/microsoft-edge",
            home / "Library/Application Support/Google/Chrome",
            home / "Library/Application Support/Microsoft Edge",
        ]
    result: list[Path] = []
    for base in candidates:
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "History").is_file():
                result.append(child)
        if (base / "History").is_file():
            result.append(base)
    # 去重，保持稳定顺序
    seen: set[str] = set()
    out: list[Path] = []
    for p in result:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _chrome_time_to_dt(value: int | float) -> dt.datetime:
    try:
        return CHROME_EPOCH + dt.timedelta(microseconds=int(value))
    except Exception:
        return CHROME_EPOCH


class BrowserHistoryReader:
    def __init__(self, max_age_days: int = 7):
        self.max_age_days = max(1, int(max_age_days))
        self._seen: set[tuple[str, str, int]] = set()   # (profile,url,yyyymmdd)

    def fetch_recent(self) -> list[BrowserVisit]:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.max_age_days)
        chrome_since = int((since - CHROME_EPOCH).total_seconds() * 1_000_000)
        visits: list[BrowserVisit] = []
        for profile in _profile_dirs():
            try:
                visits.extend(self._read_profile(profile, chrome_since))
            except Exception:
                continue
        visits.sort(key=lambda v: v.visit_time or CHROME_EPOCH, reverse=True)
        return visits

    def _read_profile(self, profile: Path, chrome_since: int) -> list[BrowserVisit]:
        history = profile / "History"
        if not history.is_file():
            return []
        tmp = Path(tempfile.gettempdir()) / f"ds-assistant-history-{uuid.uuid4().hex}.sqlite"
        try:
            # 数据库被锁时直接复制文件通常可行；失败则放弃该配置目录
            shutil.copy2(history, tmp)
            wal = profile / "History-wal"
            if wal.is_file():
                shutil.copy2(wal, Path(str(tmp) + "-wal"))
        except Exception:
            return []
        try:
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT u.url, u.title, u.visit_count, v.visit_time "
                "FROM visits v JOIN urls u ON u.id = v.url "
                "WHERE v.visit_time >= ? ORDER BY v.visit_time DESC LIMIT 300",
                (chrome_since,),
            ).fetchall()
            con.close()
        except Exception:
            return []
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(tmp) + suffix).unlink()
                except Exception:
                    pass

        out: list[BrowserVisit] = []
        for row in rows:
            url = str(row["url"] or "")
            if not url:
                continue
            domain = _domain(url)
            when = _chrome_time_to_dt(row["visit_time"])
            key = (str(profile), url, when.strftime("%Y%m%d"))
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(BrowserVisit(
                title=str(row["title"] or domain),
                url=url,
                domain=domain,
                visit_time=when,
                visit_count=int(row["visit_count"] or 1),
            ))
        return out


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url
    except Exception:
        return url


__all__ = ["BrowserVisit", "BrowserHistoryReader"]

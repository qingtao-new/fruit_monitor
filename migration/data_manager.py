from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class DatasetManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.datasets_json = self.root / "datasets.json"
        self.active_json = self.root / "active.json"

    def _load(self) -> dict[str, Any]:
        if not self.datasets_json.exists():
            return {"items": []}
        return json.loads(self.datasets_json.read_text(encoding="utf-8"))

    def _save(self, obj: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.datasets_json.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, path: str | Path) -> dict[str, Any]:
        src = Path(path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"dataset not found: {src}")
        if not src.suffix.lower() in {".csv", ".xlsx", ".xls"}:
            raise ValueError("only .csv/.xlsx/.xls files are supported")
        self.root.mkdir(parents=True, exist_ok=True)
        dataset_id = uuid.uuid4().hex[:10]
        dst = self.root / f"{dataset_id}_{src.name}"
        shutil.copyfile(src, dst)
        item = {
            "id": dataset_id,
            "filename": src.name,
            "path": str(dst),
            "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        data = self._load()
        data.setdefault("items", []).append(item)
        self._save(data)
        return item

    def list(self) -> list[dict[str, Any]]:
        data = self._load()
        items = sorted(
            data.get("items", []),
            key=lambda row: row.get("imported_at", ""),
            reverse=True,
        )
        return items

    def get(self, dataset_id: str) -> dict[str, Any]:
        for item in self._load().get("items", []):
            if item.get("id") == dataset_id:
                return item
        raise KeyError(dataset_id)

    def remove(self, dataset_id: str) -> None:
        data = self._load()
        item = None
        new_items = []
        for entry in data.get("items", []):
            if entry.get("id") == dataset_id:
                item = entry
            else:
                new_items.append(entry)
        if item is None:
            raise KeyError(dataset_id)
        data["items"] = new_items
        self._save(data)
        try:
            p = Path(item["path"])
            if p.exists():
                p.unlink()
        except Exception:
            pass
        active = self.get_active()
        if active and active.get("active_id") == dataset_id:
            self.set_active(None)

    def set_active(self, dataset_id: str | None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if dataset_id is not None and self.get(dataset_id) is None:
            raise KeyError(dataset_id)
        self.active_json.write_text(
            json.dumps({"active_id": dataset_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_active(self) -> dict[str, Any]:
        if not self.active_json.exists():
            return {"active_id": None}
        return json.loads(self.active_json.read_text(encoding="utf-8"))

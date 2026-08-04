"""[B] Collection Agent：轉檔與 readonly/mutation 分類。純程式邏輯，
對應 03a 三章「轉換流程與 baseUrl 變數化」「Readonly / Mutation 分類規則」。
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

from spec_collection_agent.exceptions import CollectionConversionError
from spec_collection_agent.postman_tree import is_folder, item_method
from spec_collection_agent.types import (
    MUTATION_METHODS,
    READONLY_METHODS,
    PostmanCollection,
)


def convert_openapi_to_postman(
    openapi_json_path: Path, output_path: Path
) -> PostmanCollection:
    """呼叫 `openapi-to-postmanv2`（經 `npx`，比照 01 八跨平台建議）把
    `openapi.json` 轉成一份原始 Collection。保留 `{{baseUrl}}` 不改寫
    （見 03a 三章「決策：保留 {{baseUrl}}」）。

    直接把字面字串 `"npx"` 交給 `subprocess.run` 在 Windows 上會失敗
    （`FileNotFoundError`）：Windows 的 npm 全域安裝把 `npx` 裝成
    `npx.cmd`，`CreateProcess` 不像 shell 那樣自動幫你補副檔名。改用
    `shutil.which()` 解析出實際可執行檔的絕對路徑（它會依 `PATHEXT`
    環境變數嘗試 `.cmd`／`.exe` 等副檔名，行為跨平台一致），解析失敗
    直接拋出清楚的錯誤，不要讓 `subprocess.run` 用一個模糊的
    `FileNotFoundError` 去掩蓋「根本沒裝 Node.js/npm」這個更根本的問題。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    npx_path = shutil.which("npx")
    if npx_path is None:
        raise FileNotFoundError(
            "系統 PATH 找不到 npx 指令，請確認 Node.js/npm 已安裝且已加入 PATH"
            "（見 00 五章環境需求）"
        )

    result = subprocess.run(
        [
            npx_path,
            "openapi-to-postmanv2",
            "-s",
            str(openapi_json_path),
            "-o",
            str(output_path),
            "-p",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",  # 不依賴作業系統預設編碼（Windows 常見非 UTF-8
        # locale，openapi-to-postmanv2 的錯誤訊息若含非 ASCII 字元會導致
        # 用系統預設編碼解碼時噴 UnicodeDecodeError）。
        stdin=subprocess.DEVNULL,  # 一次性 CLI 呼叫不需要標準輸入，明確
        # 阻斷而非繼承父進程的 stdin，避免特定終端機環境下意外卡住等輸入。
    )

    if result.returncode != 0:
        raise CollectionConversionError(
            f"openapi-to-postmanv2 轉換失敗（exit code={result.returncode}）",
            stderr=result.stderr,
        )

    with output_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _filter_tree(items: list[dict], allowed_methods: frozenset[str]) -> list[dict]:
    """遞迴保留 method 屬於 `allowed_methods` 的請求，資料夾結構不變；
    遞迴後淨空的資料夾捨棄（見 03a 三章分類規則）。
    """
    kept: list[dict] = []
    for item in items:
        if is_folder(item):
            children = _filter_tree(item["item"], allowed_methods)
            if children:
                new_folder = copy.deepcopy(item)
                new_folder["item"] = children
                kept.append(new_folder)
            continue

        method = item_method(item)
        if method in allowed_methods:
            kept.append(copy.deepcopy(item))

    return kept


def split_readonly_mutation(
    collection: PostmanCollection,
) -> tuple[PostmanCollection, PostmanCollection]:
    """依 HTTP method 把原始 Collection 拆成 readonly（GET/HEAD）/
    mutation（POST/PUT/PATCH/DELETE），見 03a 三章分類表。分類規則不做
    例外——鏈式依賴需要的額外 GET 由 `chain_dependency_inject.inject_chain_scripts()`
    另外新增，不在這裡處理。
    """
    readonly_items = _filter_tree(collection.get("item", []), READONLY_METHODS)
    mutation_items = _filter_tree(collection.get("item", []), MUTATION_METHODS)

    readonly = copy.deepcopy(collection)
    readonly["item"] = readonly_items
    readonly.setdefault("info", {})["name"] = (
        f"{collection.get('info', {}).get('name', 'api')}-readonly"
    )

    mutation = copy.deepcopy(collection)
    mutation["item"] = mutation_items
    mutation.setdefault("info", {})["name"] = (
        f"{collection.get('info', {}).get('name', 'api')}-mutation"
    )

    return readonly, mutation

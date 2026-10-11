"""Octo/CDN downloader shared by the story and home-voice menus.

Protocol reference: chihya72/gakumas-SRTSubtitle/HatsuboshiToolkit.
Only the manifest is encrypted; resourceList TXT and ACB are already raw files.
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_VERSION = 205100
API_BASE = "https://api.asset.game-gakuen-idolmaster.jp/"
APP_ID = 400
# Public Octo client parameters, not user or translation API credentials.
CLIENT_KEY = "0jv0wsohnnsigttbfigushbtl3a8m7l5"
MANIFEST_KEY = "eSquJySjayO5OLLVgdTd"


def manifest_message():
    """Describe consumed protobuf fields; unknown fields are skipped."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    schema = descriptor_pb2.FileDescriptorProto(name="octo_resources.proto", package="octo_resources", syntax="proto3")
    resource = schema.message_type.add(name="Resource")
    for number, name, kind in [(3, "name", 9), (4, "size", 5), (9, "state", 5), (11, "objectName", 9)]:
        resource.field.add(name=name, number=number, type=kind, label=1)
    database = schema.message_type.add(name="Database")
    database.field.add(name="revision", number=1, type=5, label=1)
    database.field.add(name="resourceList", number=4, type=11, label=3, type_name=".octo_resources.Resource")
    database.field.add(name="urlFormat", number=5, type=9, label=1)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("octo_resources.Database"))


def decode_manifest(content: bytes) -> dict:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    key = hashlib.sha256(MANIFEST_KEY.encode("utf-8")).digest()
    plain = unpad(AES.new(key, AES.MODE_CBC, content[:16]).decrypt(content[16:]), 16)
    database = manifest_message().FromString(plain)
    if database.revision <= 0 or "{o}" not in database.urlFormat or not database.resourceList:
        raise ValueError("Octo returned an incomplete resource manifest")
    return {"revision": database.revision, "url_format": database.urlFormat,
            "resources": [{"name": item.name, "size": item.size, "object_name": item.objectName}
                          for item in database.resourceList if item.state != 4]}


class OctoClient:
    def __init__(self, settings=None):
        try:
            import requests
            import Crypto.Cipher.AES
            import google.protobuf.message_factory
        except ImportError as error:
            raise RuntimeError("Octo 下载需要 requests、pycryptodome、protobuf>=4.25；请安装 resource-requirements.txt") from error
        self.http = requests
        self.settings = settings or {}
        self.version = int(self.settings.get("octo_version", DEFAULT_VERSION))
        self.probes = int(self.settings.get("octo_version_probes", 20))
        self.workers = int(self.settings.get("download_workers", 8))
        if self.version <= 0 or not 0 <= self.probes <= 100 or not 1 <= self.workers <= 32:
            raise ValueError("Octo 版本、探测次数或下载并发配置无效")
        self.headers = {"Accept": f"application/x-protobuf,x-octo-app/{APP_ID}", "X-OCTO-KEY": CLIENT_KEY}

    def url(self, version):
        return f"{API_BASE}v2/pub/a/{APP_ID}/v/{version}/list/0"

    def manifest(self):
        # Full list + local primary keys: failed downloads and switching from a
        # local source cannot cause an older, still-missing file to be forgotten.
        latest = None
        print(f"检查 Octo 版本（从 {self.version} 开始）…", flush=True)
        for offset in range(self.probes + 1):
            candidate = self.version + offset * 100
            with self.http.get(self.url(candidate), headers=self.headers, timeout=(10, 30), stream=True) as response:
                if response.status_code == 200:
                    latest = candidate
                elif response.status_code not in {400, 404}:
                    response.raise_for_status()
                    raise RuntimeError(f"Unexpected Octo response: {response.status_code}")
        if latest is None:
            raise RuntimeError("没有找到可用的 Octo 版本，请调整 octo_version 或检查网络")
        with self.http.get(self.url(latest), headers=self.headers, timeout=(10, 60)) as response:
            response.raise_for_status()
            result = decode_manifest(response.content)
        self.version = latest
        result["version"] = latest
        print(f"Octo 版本 {latest}，资源清单 revision {result['revision']}。", flush=True)
        return result

    def download_one(self, item, url_format, destination):
        name = item["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:txt|acb)", name):
            raise ValueError(f"Invalid resource filename: {name}")
        target = Path(destination) / name
        if target.is_file() and target.stat().st_size > 0:
            return target
        with self.http.get(url_format.replace("{o}", item["object_name"]), timeout=(10, 60)) as response:
            response.raise_for_status()
            content = response.content
        if not content or (item.get("size", 0) > 0 and len(content) != item["size"]):
            raise ValueError(f"下载文件不完整：{name}")
        if name.endswith(".acb") and not content.startswith(b"@UTF"):
            raise ValueError(f"下载内容不是 ACB：{name}")
        if name.endswith(".txt"):
            content.decode("utf-8-sig")
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            temporary.write_bytes(content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def download(self, items, manifest, destination):
        destination = Path(destination)
        if not destination.is_dir():
            raise FileNotFoundError(f"下载目标目录不存在：{destination}")
        if len({item["name"] for item in items}) != len(items):
            raise ValueError("Octo 清单中有重复的资源名")
        paths, failures = [], []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self.download_one, item, manifest["url_format"], destination): item["name"] for item in items}
            for index, future in enumerate(as_completed(futures), 1):
                name = futures[future]
                try:
                    paths.append(future.result())
                    print(f"[{index}/{len(items)}] 已保存 {name}", flush=True)
                except Exception as error:
                    failures.append(name)
                    print(f"[{index}/{len(items)}] 下载失败 {name}：{error}", flush=True)
        return sorted(paths), sorted(failures)

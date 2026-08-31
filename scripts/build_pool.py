from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "sources.yml"
OUTPUT_DIR = ROOT / "output"

VALID_POLICIES = {"github", "openwrt"}

USER_AGENT = (
    "iptv-source-pool/1.0 "
    "(+https://github.com/q743769161/iptv-source-pool)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def load_sources() -> list[dict]:
    data = yaml.safe_load(
        CONFIG_FILE.read_text(encoding="utf-8")
    ) or {}

    sources = data.get("sources")

    if not isinstance(sources, list):
        raise ValueError(
            "sources.yml 中必须存在 sources 列表"
        )

    seen_ids: set[str] = set()

    for index, source in enumerate(sources, start=1):

        if not isinstance(source, dict):
            raise ValueError(
                f"第 {index} 个 source 必须是对象"
            )

        source_id = str(
            source.get("id", "")
        ).strip()

        url = str(
            source.get("url", "")
        ).strip()

        policy = str(
            source.get("probe_policy", "")
        ).strip()

        if not source_id:
            raise ValueError(
                f"第 {index} 个 source 缺少 id"
            )

        if source_id in seen_ids:
            raise ValueError(
                f"重复 source id: {source_id}"
            )

        if not url:
            raise ValueError(
                f"{source_id} 缺少 url"
            )

        if policy not in VALID_POLICIES:
            raise ValueError(
                f"{source_id} 的 probe_policy "
                "必须是 github 或 openwrt"
            )

        seen_ids.add(source_id)

    return sources


def fetch_source(url: str) -> str:

    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT
        },
        timeout=(10, 30),
        allow_redirects=True,
    )

    response.raise_for_status()

    text = response.text.lstrip("\ufeff")

    if "#EXTINF" not in text:
        raise ValueError(
            "内容中未发现 #EXTINF，"
            "可能不是有效 M3U"
        )

    return text


def parse_m3u(
    text: str,
) -> list[tuple[str, list[str], str]]:

    entries = []

    extinf = None
    extra_lines = []

    for raw_line in text.splitlines():

        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF"):

            extinf = line
            extra_lines = []
            continue

        if extinf is None:
            continue

        if line.startswith("#"):

            extra_lines.append(line)
            continue

        entries.append(
            (
                extinf,
                extra_lines[:],
                line,
            )
        )

        extinf = None
        extra_lines = []

    return entries


def main() -> None:

    sources = load_sources()

    merged_lines = [
        "#EXTM3U"
    ]

    seen_urls = set()

    provenance = {}

    openwrt_urls = []

    statuses = []

    github_enabled = 0
    github_success = 0

    for source in sources:

        source_id = str(
            source["id"]
        )

        name = str(
            source.get(
                "name",
                source_id,
            )
        )

        url = str(
            source["url"]
        ).strip()

        enabled = bool(
            source.get(
                "enabled",
                True,
            )
        )

        policy = str(
            source["probe_policy"]
        )

        region = str(
            source.get(
                "region",
                "unknown",
            )
        )

        status = {

            "id": source_id,

            "name": name,

            "region": region,

            "probe_policy": policy,

            "enabled": enabled,

            "source_url": url,

            "checked_at": utc_now(),

            "state": (
                "disabled"
                if not enabled
                else "pending"
            ),

            "raw_entries": 0,

            "accepted_entries": 0,

            "duplicate_entries": 0,

            "error": None,
        }

        if not enabled:

            statuses.append(
                status
            )

            continue

        # =================================
        # 国内 / OpenWrt 源
        # GitHub 完全不碰
        # =================================

        if policy == "openwrt":

            openwrt_urls.append(
                url
            )

            status["state"] = (
                "deferred-to-openwrt"
            )

            statuses.append(
                status
            )

            continue

        # =================================
        # 国外 / GitHub 源
        # =================================

        github_enabled += 1

        try:

            text = fetch_source(
                url
            )

            entries = parse_m3u(
                text
            )

            status[
                "raw_entries"
            ] = len(entries)

            if not entries:

                raise ValueError(
                    "M3U 中没有解析到频道"
                )

            for (
                extinf,
                extras,
                stream_url,
            ) in entries:

                provenance.setdefault(
                    stream_url,
                    [],
                )

                if (
                    source_id
                    not in provenance[
                        stream_url
                    ]
                ):

                    provenance[
                        stream_url
                    ].append(
                        source_id
                    )

                # 完全相同 URL 自动去重

                if (
                    stream_url
                    in seen_urls
                ):

                    status[
                        "duplicate_entries"
                    ] += 1

                    continue

                seen_urls.add(
                    stream_url
                )

                merged_lines.append(
                    extinf
                )

                merged_lines.extend(
                    extras
                )

                merged_lines.append(
                    stream_url
                )

                status[
                    "accepted_entries"
                ] += 1

            status["state"] = (
                "fetched"
            )

            github_success += 1

        except Exception as exc:

            status["state"] = (
                "fetch-failed"
            )

            status["error"] = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

        statuses.append(
            status
        )

    # =====================================
    # 防止 GitHub 故障把旧结果覆盖为空
    # =====================================

    if (
        github_enabled
        and github_success == 0
    ):

        raise RuntimeError(
            "所有 probe_policy=github "
            "的源都抓取失败，"
            "停止覆盖 output"
        )

    summary = {

        "generated_at":
            utc_now(),

        "github_sources_enabled":
            github_enabled,

        "github_sources_fetched":
            github_success,

        "openwrt_sources_enabled":
            len(openwrt_urls),

        "github_unique_streams":
            len(seen_urls),

        "sources":
            statuses,
    }

    # =====================================
    # 输出 GitHub 海外候选池
    # =====================================

    atomic_write(
        OUTPUT_DIR
        / "global-merged.m3u",

        "\n".join(
            merged_lines
        ) + "\n",
    )

    # =====================================
    # 输出给 OpenWrt 的国内源地址
    # 注意：不是频道列表
    # =====================================

    atomic_write(
        OUTPUT_DIR
        / "openwrt-sources.txt",

        "\n".join(
            openwrt_urls
        )
        + (
            "\n"
            if openwrt_urls
            else ""
        ),
    )

    # =====================================
    # 源状态
    # =====================================

    atomic_write(
        OUTPUT_DIR
        / "source-status.json",

        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )

    # =====================================
    # 线路来源追踪
    # =====================================

    atomic_write(
        OUTPUT_DIR
        / "provenance.json",

        json.dumps(
            provenance,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )

    print(
        f"GitHub 源成功 "
        f"{github_success}/"
        f"{github_enabled}；"
        f"GitHub 去重后线路 "
        f"{len(seen_urls)}；"
        f"OpenWrt 待验证源 "
        f"{len(openwrt_urls)}"
    )


if __name__ == "__main__":
    main()

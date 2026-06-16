"""配布用 zip 生成スクリプト(使い捨て、配布物には含めない)。

.env / .venv / DB / __pycache__ / .git / backups / .claude を除外。
"""
import os
import zipfile

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "backups", ".claude", "node_modules"}
EXCLUDE_EXT = {".zip", ".pyc", ".pyo"}
EXCLUDE_FILES = {".env"}


def is_db(f: str) -> bool:
    return f.endswith(".db") or f.endswith(".db-wal") or f.endswith(".db-shm")


def main() -> None:
    out = "hyoka-saba-casino-bot-v1.zip"
    if os.path.exists(out):
        os.remove(out)

    count = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk("."):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
            for f in sorted(files):
                if (f in EXCLUDE_FILES or is_db(f)
                        or any(f.endswith(e) for e in EXCLUDE_EXT)
                        or f == "_make_zip.py"):  # スクリプト自身も除外
                    continue
                full = os.path.join(root, f)
                arc = os.path.relpath(full, ".").replace(os.sep, "/")
                z.write(full, arc)
                count += 1
                total += os.path.getsize(full)

    zsize = os.path.getsize(out)
    print(f"作成: {out}")
    print(f"  ファイル数: {count}")
    print(f"  元サイズ: {total / 1024:.1f} KB")
    print(f"  圧縮後  : {zsize / 1024:.1f} KB")

    # 含まれる主要ファイルの確認
    print("\n含まれる主要ファイル:")
    with zipfile.ZipFile(out) as z:
        for name in z.namelist():
            if name.endswith(("/.env", "/casino.db", "/.git")) \
                    or ".venv/" in name or "__pycache__" in name:
                print(f"  ⚠️ 漏れ: {name}")
                return
    print("  ✅ 機密ファイル(.env / *.db / .venv / .git / __pycache__) は含まれていません")


if __name__ == "__main__":
    main()

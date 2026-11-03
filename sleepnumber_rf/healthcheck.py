"""Process health probe. A powered-off base is not a failed bridge process."""

import urllib.request


def main():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8768/api/v1/health", timeout=4) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

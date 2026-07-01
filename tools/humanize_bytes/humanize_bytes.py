"""humanize_bytes — format a byte count as a human-readable string (1536 -> '1.5 KiB')."""
import argparse

_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def humanize_bytes(n):
    if n < 0:
        raise ValueError("n must be non-negative")
    size = float(n)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--selfcheck", action="store_true")
    p.add_argument("n", nargs="?", type=int, default=0)
    a = p.parse_args(argv)
    if a.selfcheck:
        assert humanize_bytes(0) == "0 B"
        assert humanize_bytes(1536) == "1.5 KiB"
        print("SELFCHECK OK")
        return 0
    print(humanize_bytes(a.n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

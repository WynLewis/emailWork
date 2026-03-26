
#!/usr/bin/env python3
import csv
import json
import pathlib
import sys

# Change this if you want a different source directory
SOURCE_DIR = pathlib.Path("data/csv")

def process_csv(csv_path: pathlib.Path, out_suffix: str = ".json") -> pathlib.Path:
    out_path = csv_path.with_suffix(out_suffix)
    # Read with UTF-8 (handles BOM via utf-8-sig) and write UTF-8 JSON
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f_in, \
         out_path.open("w", encoding="utf-8") as f_out:

        # Try to auto-detect delimiter; fall back to standard CSV
        sample = f_in.read(8192)
        f_in.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(f_in, dialect=dialect)  # first row = headers

        # Stream a JSON array (memory-friendly)
        f_out.write("[\n")
        first = True
        for row in reader:
            if not first:
                f_out.write(",\n")
            json.dump(row, f_out, ensure_ascii=False)
            first = False
        f_out.write("\n]\n")

    return out_path

def main():
    if not SOURCE_DIR.exists():
        print(f"Folder not found: {SOURCE_DIR.resolve()}", file=sys.stderr)
        sys.exit(1)

    csv_files = sorted(SOURCE_DIR.glob("*.csv"))
    if not csv_files:
        print(f"No .csv files found in {SOURCE_DIR.resolve()}")
        return

    for p in csv_files:
        try:
            out = process_csv(p)
            print(f"✓ {p.relative_to(SOURCE_DIR.parent)} -> {out.relative_to(SOURCE_DIR.parent)}")
        except Exception as e:
            print(f"✗ Failed {p.name}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
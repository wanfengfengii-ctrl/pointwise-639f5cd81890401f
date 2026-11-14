import json
import re
from pathlib import Path
from observe import load, run, verify


def verify_document(text, sources):
    match = re.search(r"<!-- metadata-evidence -->\s*\x60\x60\x60json\s*\n(.*?)\n\x60\x60\x60", text, re.S)
    if not match:
        raise AssertionError("missing metadata-evidence JSON block")
    evidence = json.loads(match.group(1))
    pack = load()
    assert set(evidence) == set(pack), "evidence cases differ"
    for name, spec in pack.items():
        item = evidence[name]
        actual = run(spec)
        assert item["actual"] == actual, (name, "actual is not reproducible")
        prediction = item["predicted"]
        assert len(prediction) == len(actual["steps"]), name
        for index, (predicted, step) in enumerate(zip(prediction, actual["steps"])):
            assert predicted["logical"] == step["logical"], (name, index, "changed behavior")
            assert isinstance(predicted["access"], str) and predicted["access"].strip(), (name, index)
    citations = re.findall(r"(lake\.py|catalog\.py|objects\.py):(\d+)", text)
    assert {name for name, line in citations} == set(sources), "cite all production modules"
    for name, line in citations:
        assert 1 <= int(line) <= len(sources[name].splitlines()), (name, line)
    return evidence


def main():
    verify()
    sources = {name: Path(name).read_text(encoding="utf-8")
               for name in ("lake.py", "catalog.py", "objects.py")}
    evidence = verify_document(Path("DESIGN.md").read_text(encoding="utf-8"), sources)
    for name, item in evidence.items():
        print(json.dumps({"case": name, "actual": item["actual"]}, ensure_ascii=False))
    print("DESIGN_EVIDENCE_OK: replay checked; access costs and design require human review")


if __name__ == "__main__":
    main()

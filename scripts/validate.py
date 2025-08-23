import json, sys, glob, os
from jsonschema import validate, Draft202012Validator

ROOT = os.path.dirname(os.path.dirname(__file__))
SCHEMA = json.load(open(os.path.join(ROOT, "schema", "survey.schema.json"), encoding="utf-8"))

errors = []

def check_dir(dirpath):
    for path in glob.glob(os.path.join(ROOT, dirpath, "*.json")):
        try:
            data = json.load(open(path, encoding="utf-8"))
            Draft202012Validator(SCHEMA).validate(data)
        except Exception as e:
            errors.append(f"{path}: {e}")

print("Checking surveys_pro/")
check_dir("surveys_pro")

# Optional config checks
for cfg in ["config/interpretations.json", "config/roles_tips.json"]:
    p = os.path.join(ROOT, cfg)
    if os.path.exists(p):
        try:
            json.load(open(p, encoding="utf-8"))
        except Exception as e:
            errors.append(f"{p}: {e}")

if errors:
    print("\nErrors:")
    print("\n".join(errors))
    sys.exit(1)

print("OK")

"""Priority ODATA REST API client."""
import json
import subprocess
import time

from lib.config import ENV


def _base_url() -> str:
    return (
        f"https://{ENV['PRIORITY_BASE_URL']}/odata/Priority/"
        f"{ENV['PRIORITY_TABULA_INI']}/{ENV['PRIORITY_COMPANY']}"
    )


def priority_url(path: str, params: str = "") -> str:
    url = f"{_base_url()}/{path}"
    if params:
        url += f"?{params}"
    return url


def priority_get(path: str, params: str = "") -> dict:
    """GET from Priority ODATA with retry."""
    url = priority_url(path, params)
    for attempt in range(3):
        result = subprocess.run(
            [
                "curl", "-s", "-k", "--connect-timeout", "30",
                "-u", f"{ENV['PRIORITY_USER']}:{ENV['PRIORITY_PASSWORD']}",
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  curl GET failed (attempt {attempt + 1}): {result.stderr}")
            time.sleep(2)
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"  Invalid JSON (attempt {attempt + 1}): {result.stdout[:200]}")
            time.sleep(2)
            continue
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            print(f"  API error: {msg}")
            return {"value": []}
        return data
    return {"value": []}


def priority_post(path: str, payload: dict) -> dict | None:
    """POST to Priority ODATA with retry. Returns parsed response or None."""
    url = priority_url(path)
    body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(3):
        result = subprocess.run(
            [
                "curl", "-s", "-k", "--connect-timeout", "30",
                "-u", f"{ENV['PRIORITY_USER']}:{ENV['PRIORITY_PASSWORD']}",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-X", "POST", "-d", body,
                url,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    curl POST failed (attempt {attempt + 1}): {result.stderr}")
            time.sleep(2)
            continue
        try:
            resp = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"    Invalid JSON (attempt {attempt + 1}): {result.stdout[:200]}")
            time.sleep(2)
            continue
        if isinstance(resp, dict) and "FORM" in resp:
            form = resp["FORM"]
            if isinstance(form, dict) and "InterfaceErrors" in form:
                err = form["InterfaceErrors"]
                text = err.get("text", str(err)) if isinstance(err, dict) else str(err)
                return {"_error": text}
        if "error" in resp:
            msg = resp["error"].get("message", str(resp["error"]))
            return {"_error": msg}
        return resp
    return {"_error": "Failed after 3 retries"}


def fetch_reference_data() -> dict:
    """Fetch all reference data from Priority. Returns a dict of lists."""
    print("\n2. Fetching Priority reference data...")

    print("   Customers...", end=" ", flush=True)
    customers = priority_get("CUSTOMERS", "$select=CUSTNAME,CUSTDES,CHANEL").get("value", [])
    print(f"{len(customers)} records")

    print("   Warehouses...", end=" ", flush=True)
    warehouses = priority_get("ZANA_WARHSDES_EXT_FL").get("value", [])
    print(f"{len(warehouses)} records")

    print("   Products (fuzzy)...", end=" ", flush=True)
    fuzzy_products = priority_get("ZANA_PARTDES_EXT_FLA").get("value", [])
    print(f"{len(fuzzy_products)} records")

    print("   Products (LOGPART)...", end=" ", flush=True)
    logpart = priority_get("LOGPART", "$select=PARTNAME,PARTDES").get("value", [])
    print(f"{len(logpart)} records")

    return {
        "customers": customers,
        "warehouses": warehouses,
        "fuzzy_products": fuzzy_products,
        "logpart": logpart,
        "customerparts": {},
    }


def fetch_customerparts(custnames: list[str]) -> dict[str, list[dict]]:
    """Fetch CUSTPART_SUBFORM for specific customers.

    Args:
        custnames: List of Priority CUSTNAME values to fetch parts for

    Returns:
        {CUSTNAME: [{"PARTNAME": ..., "PARTDES": ..., "CUSTPARTNAME": ..., "CUSTPARTDES": ...}, ...]}
    """
    result = {}
    for custname in custnames:
        parts = priority_get(
            f"CUSTOMERS('{custname}')/CUSTPART_SUBFORM",
            "$select=PARTNAME,PARTDES,CUSTPARTNAME,CUSTPARTDES",
        ).get("value", [])
        if parts:
            result[custname] = parts
    return result

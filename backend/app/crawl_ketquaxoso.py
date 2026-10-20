import json
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .crawl_http import CrawlError, allowed_url
from .etl import validate


def parse_results(raw, spec, url):
    rows, incomplete, identities = [], 0, {}
    prefix = spec["crawler"]["list_url"].removesuffix(".html") + "-ngay-"
    for article in BeautifulSoup(raw, "html.parser").select("article.lh-box"):
        heading = article.select_one("h2 a[href]")
        if heading is None:
            continue
        source = urljoin(url, heading["href"])
        if not source.startswith(prefix):
            continue
        allowed_url(source)
        title = heading.get_text(" ", strip=True)
        dates = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", title)
        ids = re.findall(r"#([0-9]{1,8})\b", title)
        if len(dates) != 1 or len(ids) != 1:
            raise CrawlError("Result block lacks an unambiguous date/draw ID")
        item = {"game_code": spec["code"], "draw_id": ids[0],
                "draw_date": datetime.strptime(dates[0], "%d/%m/%Y").date().isoformat(),
                "draw_slot": spec["slots"][0], "main": "", "special": "", "digits": "",
                "source_url": source, "verified": "false"}
        if spec["kind"] == "pick_k":
            slots = re.findall(r"\b[0-2][0-9]:[0-5][0-9]\b", title)
            if len(slots) != 1 or slots[0] not in spec["slots"]:
                raise CrawlError("Lotto result lacks an unambiguous draw slot")
            item["draw_slot"] = slots[0]
            main = article.select(".lh-balls .lh-ball:not(.lh-sp)")
            special = article.select(".lh-balls .lh-sp")
            if len(main) < spec["pick_count"] or not special:
                incomplete += 1
                continue
            if len(main) != spec["pick_count"] or len(special) != 1:
                raise CrawlError("Unexpected Lotto result ball layout")
            values = [ball.get_text(strip=True) for ball in main + special]
            if any(not re.fullmatch(r"[0-9]{1,2}", value) for value in values):
                incomplete += 1
                continue
            item["main"], item["special"] = " ".join(values[:-1]), values[-1]
        else:
            digits = {}
            labels = {"Gi\u1ea3i Nh\u1ea5t": "first", "Gi\u1ea3i Nh\u00ec": "second",
                      "Gi\u1ea3i Ba": "third", "Gi\u1ea3i Khuy\u1ebfn kh\u00edch": "consolation",
                      "Gi\u1ea3i \u0110\u1eb7c bi\u1ec7t": "special"}
            for prize in article.select("table.lh-3d tr"):
                label = prize.select_one(".lh-lb")
                group = labels.get(label.get_text(" ", strip=True)) if label else None
                if group not in spec["digit_groups"] or group in digits:
                    raise CrawlError("Unknown or duplicate digit prize group")
                digits[group] = [value.get_text(strip=True) for value in prize.select(".lh-n3")]
            if set(digits) != set(spec["digit_groups"]) or any(
                    len(digits[group]) < count or any(not re.fullmatch(r"[0-9]{3}", value) for value in digits[group])
                    for group, count in spec["digit_groups"].items() if group in digits):
                incomplete += 1
                continue
            item["digits"] = json.dumps(digits)
        row = validate(item)
        previous = identities.get(row["draw_id"])
        if previous is not None and previous != row:
            raise CrawlError("Conflicting draw IDs within source page")
        if previous is None:
            rows.append(row)
            identities[row["draw_id"]] = row
    if not rows and not incomplete:
        raise CrawlError("No recognized results; parser/source may have changed")
    return rows, incomplete


def has_next_page(raw, config, page):
    target = config["history_url"].format(page=page + 1)
    return any(urljoin(config["list_url"], link["href"]) == target
               for link in BeautifulSoup(raw, "html.parser").select("a[href]"))

import json
import re
import unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .crawl_http import CrawlError, allowed_url
from .etl import validate_source


LAYOUTS = {
    "lotto535": (".box_tructiep_vietlott_10", "ball_lotto", "ball_lotto_2"),
    "mega645": (".box_tructiep_vietlott_1", "ball_mega", None),
    "power655": (".box_tructiep_vietlott_3", "ball_power", "ball_power2"),
    "max3d": (".box_ketqua_max3d:not(.max3dpro)", None, None),
    "max3dpro": (".box_ketqua_max3d.max3dpro", None, None),
}


def pick_values(block, spec, main_class, special_class):
    balls = block.select(".box_ketqua > span.ball")
    count = spec["pick_count"] + int(spec["special_required"])
    expected_ids = [f"{spec['code']}_G{index}" for index in range(1, count + 1)]
    ids = [ball.get("id") for ball in balls]
    historical = spec["crawler"].get("historical") and all(identity is None for identity in ids)
    if len(balls) > count or (not historical and len(set(ids)) != len(balls)):
        raise CrawlError("Unexpected Minh Chinh ball layout")
    if len(balls) < count:
        return None
    if not historical and ids != expected_ids:
        raise CrawlError("Unexpected Minh Chinh ball identities/order")
    if any(main_class not in ball.get("class", []) for ball in balls[:spec["pick_count"]]):
        raise CrawlError("Minh Chinh main ball layout changed")
    if special_class and special_class not in balls[-1].get("class", []):
        raise CrawlError("Minh Chinh main/special ball layout changed")
    values = [ball.get_text(strip=True) for ball in balls]
    return values if all(re.fullmatch(r"[0-9]{1,2}", value) for value in values) else None


def digit_values(block, spec):
    cells = block.select("table.table_max3d td.max3d_number")
    groups = list(spec["digit_groups"].items())
    if len(cells) > len(groups):
        raise CrawlError("Unexpected Minh Chinh digit prize layout")
    if len(cells) < len(groups):
        return None
    labels = {"first": "giai nhat", "second": "giai nhi", "third": "giai ba",
              "consolation": "khuyen khich", "special": "dac biet"}
    if spec["code"] == "max3d":
        labels = {"first": "dac biet", "second": "giai nhat", "third": "giai nhi", "consolation": "giai ba"}
    digits = {}
    for index, (cell, (group, count)) in enumerate(zip(cells, groups), 1):
        label = cell.find_previous_sibling("td")
        text = unicodedata.normalize("NFD", label.get_text(" ", strip=True).lower()) if label else ""
        text = " ".join("".join(char for char in text if not unicodedata.combining(char)).replace("\u0111", "d").split())
        if labels[group] not in text:
            raise CrawlError("Minh Chinh digit prize labels/order changed")
        values = cell.select("div[id]")
        if len(values) > count:
            raise CrawlError("Unexpected Minh Chinh digit group count")
        if len(values) < count:
            return None
        if [value.get("id") for value in values] != [f"max3d_G{index}_{position}" for position in range(1, count + 1)]:
            raise CrawlError("Unexpected Minh Chinh digit identities/order")
        digits[group] = [value.get_text(strip=True) for value in values]
        if any(not re.fullmatch(r"[0-9]{3}", value) for value in digits[group]):
            return None
    return digits


def parse_results(raw, spec, url):
    if spec["code"] not in LAYOUTS:
        raise CrawlError("Unsupported Minh Chinh game")
    selector, main_class, special_class = LAYOUTS[spec["code"]]
    rows, incomplete, identities = [], 0, {}
    soup = BeautifulSoup(raw, "html.parser")
    for block in soup.select(".boxketqua_vl" + selector):
        heading = block.select_one(".content_vl > .ngay")
        if heading is None:
            raise CrawlError("Minh Chinh result heading is missing")
        title = heading.get_text(" ", strip=True)
        ids = re.findall(r"#([0-9]{1,8})\b", title)
        dates = re.findall(r"\b[0-9]{2}/[0-9]{2}/[0-9]{4}\b", title)
        slots = re.findall(r"\b[0-2][0-9]:[0-5][0-9]\b", title)
        if not slots and len(spec["slots"]) == 1:
            slots = spec["slots"]
        if len(ids) != 1 or len(dates) != 1 or len(slots) != 1:
            raise CrawlError("Minh Chinh result lacks an unambiguous draw ID/date/slot")
        link = heading.select_one("a[href]")
        source = urljoin(url, link["href"]) if link else url
        allowed_url(source)
        if urlsplit(source).hostname != urlsplit(url).hostname:
            raise CrawlError("Minh Chinh result link points to another source")
        item = {"game_code": spec["code"], "draw_id": ids[0],
                "draw_date": datetime.strptime(dates[0], "%d/%m/%Y").date().isoformat(),
                "draw_slot": slots[0], "main": "", "special": "", "digits": "",
                "source_url": source, "verified": "false"}
        if spec["kind"] == "pick_k":
            values = pick_values(block, spec, main_class, special_class)
            if values is None:
                incomplete += 1
                continue
            item["main"] = " ".join(values[:spec["pick_count"]])
            item["special"] = values[-1] if spec["special_required"] else ""
        else:
            digits = digit_values(block, spec)
            if digits is None:
                incomplete += 1
                continue
            item["digits"] = json.dumps(digits)
        row = validate_source(item, spec)
        previous = identities.get(row["draw_id"])
        if previous is not None and previous != row:
            raise CrawlError("Conflicting draw IDs within Minh Chinh page")
        if previous is None:
            rows.append(row)
            identities[row["draw_id"]] = row
    if not rows and not incomplete:
        raise CrawlError("No recognized Minh Chinh results; source may be blocked or changed")
    return rows, incomplete

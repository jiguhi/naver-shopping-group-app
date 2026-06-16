
import streamlit as st
import pandas as pd
import requests
import time
import hmac
import hashlib
import base64
import re
from collections import defaultdict
from datetime import datetime
import json
import os
BASE_URL = "https://api.searchad.naver.com"
st.write("VERSION 2026-06-16-02")
SETTING_FILE = "settings.json"
MAX_ADS_PER_GROUP = 1000
RESULT_FILE = "progress_result.csv"


def load_progress():
    if os.path.exists(RESULT_FILE):
        try:
            return pd.read_csv(RESULT_FILE, dtype=str, encoding="utf-8-sig")
        except:
            pass
    return pd.DataFrame()

def save_progress_row(row):
    df_row = pd.DataFrame([row])
    file_exists = os.path.exists(RESULT_FILE)

    df_row.to_csv(
        RESULT_FILE,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8-sig"
    )
    
def get_existing_ads_with_count(api_key, secret_key, customer_id, adgroup_id):
    uri = "/ncc/ads"
    params = {"nccAdgroupId": adgroup_id}

    res = api_request(api_key, secret_key, customer_id, "GET", uri, params=params)

    if not res.ok:
        return set(), 0

    existing_refs = set()
    ads = res.json()

    for ad in ads:
        ref = str(ad.get("referenceKey", "")).strip()
        if ref:
            existing_refs.add(ref)

    return existing_refs, len(ads)

def make_next_group_name(base_group_name, group_map):
    idx = 1

    while True:
        new_name = f"{base_group_name} {idx}"

        if new_name not in group_map:
            return new_name

        idx += 1



def find_available_group(
    api_key,
    secret_key,
    customer_id,
    base_group_name,
    group_map
):
    candidate_names = [base_group_name]

    idx = 1
    while True:
        name = f"{base_group_name} {idx}"

        if name in group_map:
            candidate_names.append(name)
            idx += 1
        else:
            break

    last_existing_name = candidate_names[-1]
    last_group_info = group_map.get(last_existing_name)
    last_adgroup_id = last_group_info.get("nccAdgroupId")

    existing_refs, ad_count = get_existing_ads_with_count(
        api_key,
        secret_key,
        customer_id,
        last_adgroup_id
    )

    if ad_count < MAX_ADS_PER_GROUP:
        return {
            "group_name": last_existing_name,
            "adgroup_id": last_adgroup_id,
            "existing_refs": existing_refs,
            "ad_count": ad_count,
            "need_create": False,
            "new_group_name": None
        }

    new_group_name = make_next_group_name(base_group_name, group_map)

    return {
        "group_name": new_group_name,
        "adgroup_id": None,
        "existing_refs": set(),
        "ad_count": 0,
        "need_create": True,
        "new_group_name": new_group_name
    }

def load_settings():
    if os.path.exists(SETTING_FILE):
        try:
            with open(SETTING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass

    return {
        "api_key": "",
        "secret_key": "",
        "customer_id": "",
        "campaign_id": ""
    }


def save_settings(api_key, secret_key, customer_id, campaign_id):
    data = {
        "api_key": api_key,
        "secret_key": secret_key,
        "customer_id": customer_id,
        "campaign_id": campaign_id
    }

    with open(SETTING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# =========================
# 인증 / API 요청
# =========================

def generate_signature(timestamp, method, uri, secret_key):
    message = f"{timestamp}.{method}.{uri}"
    hash_value = hmac.new(
        bytes(secret_key, "utf-8"),
        bytes(message, "utf-8"),
        hashlib.sha256
    )
    return base64.b64encode(hash_value.digest()).decode()


def get_header(api_key, secret_key, customer_id, method, uri):
    timestamp = str(round(time.time() * 1000))
    signature = generate_signature(timestamp, method, uri, secret_key)

    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": str(customer_id),
        "X-Signature": signature
    }


def api_request(api_key, secret_key, customer_id, method, uri, params=None, json_data=None):
    url = BASE_URL + uri
    sign_uri = uri.split("?")[0]

    wait_list = [10, 20, 40, 60, 90, 120]

    last_res = None

    for retry, wait in enumerate(wait_list, start=1):
        headers = get_header(api_key, secret_key, customer_id, method, sign_uri)

        res = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            timeout=30
        )

        last_res = res

        if res.status_code == 400 and "1014" in res.text:
            time.sleep(wait)
            continue

        return res

    return last_res

# =========================
# 상품 / 카테고리 처리
# =========================

def get_col(row, keyword):
    keyword_clean = keyword.replace(" ", "")

    for col in row.index:
        col_clean = str(col).replace(" ", "")
        if keyword_clean in col_clean:
            return row.get(col, "")

    return ""


def make_category_name(row):
    cats = [
        row.get("대분류", ""),
        row.get("중분류", ""),
        row.get("소분류", ""),
        row.get("세분류", "")
    ]

    cats = [
        str(c).strip()
        for c in cats
        if str(c).strip() and str(c).strip().lower() != "nan"
    ]

    return " > ".join(cats) if cats else "카테고리없음"


def clean_group_name(name):
    name = str(name)
    name = name.replace(">", "-")
    name = name.replace("/", "-")
    name = re.sub(r"[^가-힣a-zA-Z0-9\s_-]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\s*-\s*", "-", name)
    return name[:120]


def load_products_from_df(df):
    # 판매/전시 상태 필터
    if "판매상태" in df.columns:
        df = df[df["판매상태"].astype(str).str.contains("판매", na=False)]

    if "전시상태" in df.columns:
        df = df[df["전시상태"].astype(str).str.contains("전시", na=False)]

    products = []

    for _, row in df.iterrows():
        # 우선 네이버쇼핑상품번호 사용, 없으면 상품번호 fallback
        shopping_product_no = str(
            get_col(row, "네이버쇼핑상품번호(스마트스토어)")
        ).strip()

        if not shopping_product_no or shopping_product_no.lower() == "nan":
            shopping_product_no = str(
                get_col(row, "상품번호(스마트스토어)")
            ).strip()

        product_no = str(
            get_col(row, "상품번호(스마트스토어)")
        ).strip()

        product_name = str(row.get("상품명", "")).strip()
        category_name = make_category_name(row)

        if not shopping_product_no or shopping_product_no.lower() == "nan":
            continue

        products.append({
            "product_no": product_no,
            "shopping_product_no": shopping_product_no,
            "product_name": product_name,
            "category_name": category_name,
            "group_name": clean_group_name(category_name)
        })

    return products


def group_products(products):
    grouped = defaultdict(list)
    for product in products:
        grouped[product["category_name"]].append(product)
    return grouped


# =========================
# 네이버 광고그룹 / 소재
# =========================

def get_adgroups(api_key, secret_key, customer_id):
    uri = "/ncc/adgroups"
    res = api_request(api_key, secret_key, customer_id, "GET", uri)

    if not res.ok:
        raise Exception(f"광고그룹 조회 실패: {res.status_code} / {res.text}")

    return res.json()


def get_adgroup_map_by_campaign(api_key, secret_key, customer_id, campaign_id):
    groups = get_adgroups(api_key, secret_key, customer_id)

    group_map = {}

    for g in groups:
        if g.get("nccCampaignId") == campaign_id:
            group_map[g.get("name")] = {
                "nccAdgroupId": g.get("nccAdgroupId"),
                "pcChannelId": g.get("pcChannelId"),
                "mobileChannelId": g.get("mobileChannelId"),
                "pcChannelKey": g.get("pcChannelKey"),
                "mobileChannelKey": g.get("mobileChannelKey"),
                "adgroupType": g.get("adgroupType")
            }

    return group_map


def get_channel_ids_from_same_campaign(api_key, secret_key, customer_id, campaign_id):
    groups = get_adgroups(api_key, secret_key, customer_id)

    for g in groups:
        if g.get("nccCampaignId") == campaign_id and g.get("adgroupType") == "SHOPPING":
            pc = g.get("pcChannelId")
            mobile = g.get("mobileChannelId")

            if pc and mobile:
                return {
                    "base_group_name": g.get("name"),
                    "pcChannelId": pc,
                    "mobileChannelId": mobile,
                    "pcChannelKey": g.get("pcChannelKey"),
                    "mobileChannelKey": g.get("mobileChannelKey"),
                }

    raise Exception("같은 캠페인 안에 SHOPPING 기준 광고그룹이 없습니다. 광고센터에서 쇼핑검색 광고그룹 1개를 먼저 수동 생성하세요.")


def create_adgroup(api_key, secret_key, customer_id, campaign_id, group_name, pc_channel_id, mobile_channel_id, group_bid_amt, contents_bid_amt):
    uri = "/ncc/adgroups"

    payload = {
        "name": group_name,
        "nccCampaignId": campaign_id,
        "adgroupType": "SHOPPING",
        "bidAmt": int(group_bid_amt),
        "contentsNetworkBidAmt": int(contents_bid_amt),
        "contentsNetworkBidWeight": 100,
        "mobileNetworkBidWeight": 100,
        "pcNetworkBidWeight": 100,
        "dailyBudget": 0,
        "userLock": False,
        "pcChannelId": pc_channel_id,
        "mobileChannelId": mobile_channel_id
    }

    res = api_request(api_key, secret_key, customer_id, "POST", uri, json_data=payload)
    return res


def create_missing_adgroups(api_key, secret_key, customer_id, campaign_id, grouped, group_bid_amt, contents_bid_amt, log_box):
    channels = get_channel_ids_from_same_campaign(api_key, secret_key, customer_id, campaign_id)

    log_box.write(f"기준 광고그룹: {channels['base_group_name']}")
    log_box.write(f"채널: {channels.get('pcChannelKey')}")

    group_map = get_adgroup_map_by_campaign(api_key, secret_key, customer_id, campaign_id)

    created_rows = []

    progress = st.progress(0)
    total = len(grouped)

    for idx, category_name in enumerate(grouped.keys(), start=1):
        group_name = clean_group_name(category_name)

        if group_name in group_map:
            adgroup_id = group_map[group_name]["nccAdgroupId"]
            log_box.write(f"[기존 그룹 사용] {group_name} / {adgroup_id}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": adgroup_id,
                "group_status": "기존"
            })
            progress.progress(idx / total)
            continue

        res = create_adgroup(
            api_key=api_key,
            secret_key=secret_key,
            customer_id=customer_id,
            campaign_id=campaign_id,
            group_name=group_name,
            pc_channel_id=channels["pcChannelId"],
            mobile_channel_id=channels["mobileChannelId"],
            group_bid_amt=group_bid_amt,
            contents_bid_amt=contents_bid_amt
        )

        if res.ok:
            data = res.json()
            adgroup_id = data.get("nccAdgroupId")
            group_map[group_name] = {
                "nccAdgroupId": adgroup_id,
                "pcChannelId": channels["pcChannelId"],
                "mobileChannelId": channels["mobileChannelId"],
                "adgroupType": "SHOPPING"
            }
            log_box.write(f"[그룹 생성 완료] {group_name} / {adgroup_id}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": adgroup_id,
                "group_status": "생성"
            })
        else:
            log_box.write(f"[그룹 생성 실패] {group_name} / {res.status_code} / {res.text}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": "",
                "group_status": f"실패: {res.status_code} {res.text}"
            })

        progress.progress(idx / total)
        time.sleep(1)

    return group_map, created_rows


def get_existing_ads(api_key, secret_key, customer_id, adgroup_id):
    uri = "/ncc/ads"
    params = {"nccAdgroupId": adgroup_id}

    res = api_request(api_key, secret_key, customer_id, "GET", uri, params=params)

    if not res.ok:
        return set()

    existing_refs = set()

    for ad in res.json():
        ref = str(ad.get("referenceKey", "")).strip()
        if ref:
            existing_refs.add(ref)

    return existing_refs


def create_shopping_product_ad(api_key, secret_key, customer_id, adgroup_id, shopping_product_no, group_bid_amt):
    uri = "/ncc/ads?isList=true"

    # 네이버 쇼핑상품 소재 등록 구조
    payload = [
        {
            "nccAdgroupId": adgroup_id,
            "type": "SHOPPING_PRODUCT_AD",
            "referenceKey": str(shopping_product_no),
            "ad": {},
            "adAttr": {
                "bidAmt": int(group_bid_amt),
                "useGroupBidAmt": True
            },
            "userLock": False
        }
    ]

    res = api_request(api_key, secret_key, customer_id, "POST", uri, json_data=payload)
    return res

def register_products_to_adgroups(
    api_key,
    secret_key,
    customer_id,
    campaign_id,
    grouped,
    group_map,
    group_bid_amt,
    contents_bid_amt,
    log_box
):
    result_rows = []
    progress_df = load_progress()

    done_refs = set()
    if not progress_df.empty and "shopping_product_no" in progress_df.columns:
        done_refs = set(
            progress_df[
                progress_df["status"].isin(["등록완료", "중복스킵"])
            ]["shopping_product_no"].astype(str)
        )
    success_count = 0
    fail_count = 0
    skip_count = 0

    all_products_count = sum(len(v) for v in grouped.values())
    current = 0
    progress = st.progress(0)

    channels = get_channel_ids_from_same_campaign(
        api_key,
        secret_key,
        customer_id,
        campaign_id
    )

    for category_name, products in grouped.items():
        base_group_name = clean_group_name(category_name)

        group_info = group_map.get(base_group_name)

        if not group_info:
            log_box.write(f"[스킵] 광고그룹 없음: {base_group_name}")

            for product in products:
                skip_count += 1
                current += 1

                result_rows.append({
                    "category_name": category_name,
                    "group_name": base_group_name,
                    "adgroup_id": "",
                    "shopping_product_no": product["shopping_product_no"],
                    "product_name": product["product_name"],
                    "status": "스킵",
                    "message": "광고그룹 없음"
                })

                progress.progress(current / all_products_count)

            continue

        available = find_available_group(
            api_key,
            secret_key,
            customer_id,
            base_group_name,
            group_map
        )

        active_group_name = available["group_name"]
        adgroup_id = available["adgroup_id"]
        existing_refs = available["existing_refs"]
        ad_count = available["ad_count"]

        if available["need_create"]:
            log_box.write(f"[기존 확장그룹도 1000개 도달] 새 광고그룹 생성: {active_group_name}")

            res_group = create_adgroup(
                api_key=api_key,
                secret_key=secret_key,
                customer_id=customer_id,
                campaign_id=campaign_id,
                group_name=active_group_name,
                pc_channel_id=channels["pcChannelId"],
                mobile_channel_id=channels["mobileChannelId"],
                group_bid_amt=group_bid_amt,
                contents_bid_amt=contents_bid_amt
            )

            if res_group.ok:
                new_data = res_group.json()
                adgroup_id = new_data.get("nccAdgroupId")

                group_map[active_group_name] = {
                    "nccAdgroupId": adgroup_id,
                    "pcChannelId": channels["pcChannelId"],
                    "mobileChannelId": channels["mobileChannelId"],
                    "adgroupType": "SHOPPING"
                }

                existing_refs = set()
                ad_count = 0

                log_box.write(f"[새 그룹 생성 완료] {active_group_name} / {adgroup_id}")

            else:
                log_box.write(
                    f"[새 그룹 생성 실패] {active_group_name} / "
                    f"{res_group.status_code} / {res_group.text}"
                )

                for product in products:
                    fail_count += 1
                    current += 1

                    result_rows.append({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": "",
                        "shopping_product_no": product["shopping_product_no"],
                        "product_name": product["product_name"],
                        "status": "등록실패",
                        "message": f"새 광고그룹 생성 실패: {res_group.status_code} / {res_group.text}"
                    })

                    progress.progress(current / all_products_count)

                continue

        log_box.write(f"상품 등록 시작: {active_group_name} / {adgroup_id} / 현재 소재수 {ad_count}")

        for product in products:
            current += 1
            shopping_no = str(product["shopping_product_no"]).strip()
            product_name = product["product_name"]
            if shopping_no in done_refs:
                skip_count += 1
                log_box.write(f"[이미 처리됨 스킵] {shopping_no} / {product_name}")
                progress.progress(current / all_products_count)
                continue
            if not shopping_no or shopping_no.lower() == "nan":
                skip_count += 1
            
                row = {
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "스킵",
                    "message": "상품번호 없음",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            
                result_rows.append(row)
                save_progress_row(row)
            
                progress.progress(current / all_products_count)
                continue

            if shopping_no in existing_refs:
                skip_count += 1
            
                log_box.write(f"[중복 스킵] {shopping_no} / {product_name}")
            
                row = {
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "중복스킵",
                    "message": "이미 등록된 상품",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            
                result_rows.append(row)
                save_progress_row(row)
            
                progress.progress(current / all_products_count)
                continue
            if ad_count >= MAX_ADS_PER_GROUP:
                active_group_name = make_next_group_name(base_group_name, group_map)

                log_box.write(f"[소재 1000개 도달] 새 광고그룹 생성: {active_group_name}")

                res_group = create_adgroup(
                    api_key=api_key,
                    secret_key=secret_key,
                    customer_id=customer_id,
                    campaign_id=campaign_id,
                    group_name=active_group_name,
                    pc_channel_id=channels["pcChannelId"],
                    mobile_channel_id=channels["mobileChannelId"],
                    group_bid_amt=group_bid_amt,
                    contents_bid_amt=contents_bid_amt
                )

                if res_group.ok:
                    new_data = res_group.json()
                    adgroup_id = new_data.get("nccAdgroupId")

                    group_map[active_group_name] = {
                        "nccAdgroupId": adgroup_id,
                        "pcChannelId": channels["pcChannelId"],
                        "mobileChannelId": channels["mobileChannelId"],
                        "adgroupType": "SHOPPING"
                    }

                    existing_refs = set()
                    ad_count = 0

                    log_box.write(f"[새 그룹 생성 완료] {active_group_name} / {adgroup_id}")

                else:
                    fail_count += 1
                    if res.status_code == 400 and "1014" in res.text:
                        log_box.write("[호출 제한 초과] API 제한이 풀리지 않아 실행을 중단합니다. 잠시 후 다시 실행해주세요.")
                        st.stop()
                    log_box.write(
                        f"[새 그룹 생성 실패] {active_group_name} / "
                        f"{res_group.status_code} / {res_group.text}"
                    )

                    result_rows.append({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": "",
                        "shopping_product_no": shopping_no,
                        "product_name": product_name,
                        "status": "등록실패",
                        "message": f"새 광고그룹 생성 실패: {res_group.status_code} / {res_group.text}"
                    })

                    progress.progress(current / all_products_count)
                    continue

            res = create_shopping_product_ad(
                api_key=api_key,
                secret_key=secret_key,
                customer_id=customer_id,
                adgroup_id=adgroup_id,
                shopping_product_no=shopping_no,
                group_bid_amt=group_bid_amt
            )

            if res.ok:
                success_count += 1
                existing_refs.add(shopping_no)
                ad_count += 1
            
                log_box.write(f"[등록 완료] {shopping_no} / {product_name}")
            
                row = {
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "등록완료",
                    "message": res.text,
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            
                result_rows.append(row)
                save_progress_row(row)
            else:
                fail_count += 1
            
                if res.status_code == 400 and "1014" in res.text:
                    log_box.write(
                        "[호출 제한 초과] API 제한이 풀리지 않아 작업을 종료합니다."
                    )
                    st.stop()
            
                log_box.write(
                    f"[등록 실패] {shopping_no} / {product_name} / "
                    f"{res.status_code} / {res.text}"
                )

                row = {
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "등록실패",
                    "message": f"{res.status_code} / {res.text}",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                result_rows.append(row)
                save_progress_row(row)

            progress.progress(current / all_products_count)
            time.sleep(3)

    summary = {
        "success_count": success_count,
        "fail_count": fail_count,
        "skip_count": skip_count
    }

    return result_rows, summary
# =========================
# Streamlit UI
# =========================

st.set_page_config(
    page_title="네이버 쇼핑검색 자동 그룹/상품 등록",
    layout="wide"
)

st.title("네이버 쇼핑검색 카테고리별 광고그룹 생성 & 상품 등록")

st.warning(
    "API Key/Secret Key는 화면 입력값으로만 사용됩니다. "
    "코드에 직접 저장하지 않는 것을 권장합니다."
)

with st.sidebar:
    st.header("1. API 설정")

    settings = load_settings()

    api_key = st.text_input(
        "API Key",
        value=settings.get("api_key", ""),
        type="password"
    )
    
    secret_key = st.text_input(
        "Secret Key",
        value=settings.get("secret_key", ""),
        type="password"
    )
    
    customer_id = st.text_input(
        "Customer ID",
        value=settings.get("customer_id", "")
    )
    
    campaign_id = st.text_input(
        "Campaign ID",
        value=settings.get("campaign_id", "")
    )

    st.header("2. 입찰가 설정")
    group_bid_amt = st.number_input("광고그룹 기본 입찰가", min_value=70, value=200, step=10)
    contents_bid_amt = st.number_input("콘텐츠 네트워크 입찰가", min_value=70, value=200, step=10)
    

    st.header("3. 실행 옵션")
    run_create_groups = st.checkbox("카테고리별 광고그룹 생성", value=True)
    run_register_products = st.checkbox("각 광고그룹에 상품 등록", value=True)

uploaded_file = st.file_uploader("상품 CSV 파일 업로드", type=["csv"])

df = None
products = []
grouped = {}

if uploaded_file:
    df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
    products = load_products_from_df(df)
    grouped = group_products(products)

    col1, col2, col3 = st.columns(3)
    col1.metric("CSV 전체 행", f"{len(df):,}")
    col2.metric("등록 대상 상품", f"{len(products):,}")
    col3.metric("생성 대상 카테고리", f"{len(grouped):,}")

    st.subheader("CSV 컬럼")
    st.write(list(df.columns))

    preview_df = pd.DataFrame([
        {
            "카테고리": category,
            "광고그룹명": clean_group_name(category),
            "상품수": len(items)
        }
        for category, items in grouped.items()
    ])

    st.subheader("카테고리별 광고그룹 미리보기")
    st.dataframe(preview_df, use_container_width=True)

    with st.expander("상품 미리보기"):
        st.dataframe(pd.DataFrame(products).head(100), use_container_width=True)

col_test, col_run = st.columns([1, 2])

with col_test:
    if st.button("API 연결 테스트"):
        if not all([api_key, secret_key, customer_id]):
            st.error("API Key, Secret Key, Customer ID를 입력해주세요.")
        else:
            try:
                res = api_request(api_key, secret_key, customer_id, "GET", "/ncc/adgroups")
                if res.ok:
                    st.success("API 연결 성공")
                    st.write(f"광고그룹 수: {len(res.json())}")
                else:
                    st.error(f"API 연결 실패: {res.status_code} / {res.text}")
            except Exception as e:
                st.error(str(e))

with col_run:
    run_btn = st.button("광고그룹 생성 및 상품 등록 실행", type="primary")

if run_btn:
    save_settings(
        api_key,
        secret_key,
        customer_id,
        campaign_id
        )
    if not uploaded_file:
        st.error("CSV 파일을 업로드해주세요.")
    elif not all([api_key, secret_key, customer_id, campaign_id]):
        st.error("API Key, Secret Key, Customer ID, Campaign ID를 모두 입력해주세요.")
    elif not run_create_groups and not run_register_products:
        st.error("실행 옵션을 최소 1개 이상 선택해주세요.")
    else:
        log_box = st.empty()
        log_text = []

        class LogBox:
            def write(self, msg):
                log_text.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
                log_box.code("\n".join(log_text[-80:]))

        logger = LogBox()

        try:
            group_result_rows = []
            register_result_rows = []

            if run_create_groups:
                st.subheader("1. 광고그룹 생성")
                group_map, group_result_rows = create_missing_adgroups(
                    api_key=api_key,
                    secret_key=secret_key,
                    customer_id=customer_id,
                    campaign_id=campaign_id,
                    grouped=grouped,
                    group_bid_amt=group_bid_amt,
                    contents_bid_amt=contents_bid_amt,
                    log_box=logger
                )
            else:
                group_map = get_adgroup_map_by_campaign(
                    api_key=api_key,
                    secret_key=secret_key,
                    customer_id=customer_id,
                    campaign_id=campaign_id
                )

            if run_register_products:
                st.subheader("2. 상품 소재 등록")
                register_result_rows, summary = register_products_to_adgroups(
                    api_key=api_key,
                    secret_key=secret_key,
                    customer_id=customer_id,
                    campaign_id=campaign_id,
                    grouped=grouped,
                    group_map=group_map,
                    group_bid_amt=group_bid_amt,
                    contents_bid_amt=contents_bid_amt,
                    log_box=logger
                )

                st.success("실행 완료")
                c1, c2, c3 = st.columns(3)
                c1.metric("등록 성공", summary["success_count"])
                c2.metric("등록 실패", summary["fail_count"])
                c3.metric("중복/스킵", summary["skip_count"])
                total_count = (
                summary["success_count"]
                + summary["fail_count"]
                + summary["skip_count"]
            )
            
            success_rate = (
                summary["success_count"] / total_count * 100
                if total_count > 0 else 0
            )
            
            st.subheader("작업 결과 리포트")
            
            report_df = pd.DataFrame([
                {
                    "항목": "전체 처리 상품 수",
                    "결과": f"{total_count:,}개"
                },
                {
                    "항목": "등록 성공",
                    "결과": f"{summary['success_count']:,}개"
                },
                {
                    "항목": "등록 실패",
                    "결과": f"{summary['fail_count']:,}개"
                },
                {
                    "항목": "중복/스킵",
                    "결과": f"{summary['skip_count']:,}개"
                },
                {
                    "항목": "성공률",
                    "결과": f"{success_rate:.1f}%"
                },
                {
                    "항목": "작업 완료 시간",
                    "결과": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            ])

            st.dataframe(report_df, width="stretch")
            st.download_button(
                "작업 결과 리포트 CSV 다운로드",
                report_df.to_csv(index=False, encoding="utf-8-sig"),
                file_name="naver_shopping_work_summary.csv",
                mime="text/csv"
            )
            if group_result_rows:
                st.subheader("광고그룹 생성 결과")
                group_df = pd.DataFrame(group_result_rows)
                st.dataframe(group_df, use_container_width=True)
                st.download_button(
                    "광고그룹 생성 결과 CSV 다운로드",
                    group_df.to_csv(index=False, encoding="utf-8-sig"),
                    file_name="naver_adgroup_create_result.csv",
                    mime="text/csv"
                )

            if register_result_rows:
                st.subheader("상품 등록 결과")
                result_df = pd.DataFrame(register_result_rows)
                st.dataframe(result_df, use_container_width=True)
                st.download_button(
                    "상품 등록 결과 CSV 다운로드",
                    result_df.to_csv(index=False, encoding="utf-8-sig"),
                    file_name="naver_product_register_result.csv",
                    mime="text/csv"
                )

        except Exception as e:
            st.error(f"실행 중 오류 발생: {e}")

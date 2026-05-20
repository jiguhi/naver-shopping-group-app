
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

BASE_URL = "https://api.searchad.naver.com"


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

    # 네이버 검색광고 서명에는 ?isList=true 같은 쿼리스트링 제외
    sign_uri = uri.split("?")[0]
    headers = get_header(api_key, secret_key, customer_id, method, sign_uri)

    res = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_data,
        timeout=30
    )
    return res


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
        time.sleep(0.25)

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


def register_products_to_adgroups(api_key, secret_key, customer_id, grouped, group_map, group_bid_amt, log_box):
    result_rows = []
    success_count = 0
    fail_count = 0
    skip_count = 0

    all_products_count = sum(len(v) for v in grouped.values())
    current = 0
    progress = st.progress(0)

    for category_name, products in grouped.items():
        group_name = clean_group_name(category_name)
        group_info = group_map.get(group_name)
        adgroup_id = group_info.get("nccAdgroupId") if group_info else None

        if not adgroup_id:
            log_box.write(f"[스킵] 광고그룹 없음: {group_name}")
            for product in products:
                skip_count += 1
                current += 1
                result_rows.append({
                    "category_name": category_name,
                    "group_name": group_name,
                    "adgroup_id": "",
                    "shopping_product_no": product["shopping_product_no"],
                    "product_name": product["product_name"],
                    "status": "스킵",
                    "message": "광고그룹 없음"
                })
            continue

        log_box.write(f"상품 등록 시작: {group_name} / {adgroup_id}")
        existing_refs = get_existing_ads(api_key, secret_key, customer_id, adgroup_id)

        for product in products:
            current += 1
            shopping_no = str(product["shopping_product_no"]).strip()
            product_name = product["product_name"]

            if not shopping_no or shopping_no.lower() == "nan":
                skip_count += 1
                result_rows.append({
                    "category_name": category_name,
                    "group_name": group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "스킵",
                    "message": "상품번호 없음"
                })
                progress.progress(current / all_products_count)
                continue

            if shopping_no in existing_refs:
                skip_count += 1
                log_box.write(f"[중복 스킵] {shopping_no} / {product_name}")
                result_rows.append({
                    "category_name": category_name,
                    "group_name": group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "중복스킵",
                    "message": "이미 등록된 상품"
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
                log_box.write(f"[등록 완료] {shopping_no} / {product_name}")
                result_rows.append({
                    "category_name": category_name,
                    "group_name": group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "등록완료",
                    "message": res.text
                })
            else:
                fail_count += 1
                log_box.write(f"[등록 실패] {shopping_no} / {product_name} / {res.status_code} / {res.text}")
                result_rows.append({
                    "category_name": category_name,
                    "group_name": group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "등록실패",
                    "message": f"{res.status_code} / {res.text}"
                })

            progress.progress(current / all_products_count)
            time.sleep(0.25)

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

    api_key = st.text_input("API Key", type="password")
    secret_key = st.text_input("Secret Key", type="password")
    customer_id = st.text_input("Customer ID")
    campaign_id = st.text_input("Campaign ID")

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
                    grouped=grouped,
                    group_map=group_map,
                    group_bid_amt=group_bid_amt,
                    log_box=logger
                )

                st.success("실행 완료")
                c1, c2, c3 = st.columns(3)
                c1.metric("등록 성공", summary["success_count"])
                c2.metric("등록 실패", summary["fail_count"])
                c3.metric("중복/스킵", summary["skip_count"])

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

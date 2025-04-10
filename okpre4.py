# -*- coding: utf-8 -*-
import streamlit as st
st.set_page_config(page_title="Okosy - 自分らしい旅をデザイン", layout="wide")
st.title("Okosy - 自分らしい旅をデザイン")
st.caption("SNSや広告にハックされない、“本来の旅”を取り戻す")
import sqlite3
import openai
# ★★★ OpenAI v1.x 対応: OpenAI クラスをインポート ★★★
from openai import OpenAI
import requests
import json
import os
import datetime
from dotenv import load_dotenv
from PIL import Image
import io
import pandas as pd

# --- 1. 環境変数の読み込みと初期設定 ---
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

if not OPENAI_API_KEY:
    st.error("OpenAI APIキーが見つかりません。.envファイルを確認してください。")
    st.stop()

if not GOOGLE_PLACES_API_KEY:
    st.error("Google Places APIキーが見つかりません。.envファイルを確認してください。")
    st.stop()

# ★★★ OpenAI v1.x 対応: クライアントを初期化 ★★★
# 環境変数 OPENAI_API_KEY は自動的に読み込まれます
client = OpenAI()

# --- 2. データベースの初期設定 (SQLite) ---
DATABASE_NAME = "okosy_data_noauth.db"

def get_db_connection():
    """SQLiteデータベースへのコネクションを取得する"""
    return sqlite3.connect(DATABASE_NAME)

def init_db():
    """
    データベースのテーブルが存在しなければ作成する
    - しおり(itineraries)
    - 思い出(memories)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    # しおりテーブル (usernameなし)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS itineraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            preferences TEXT,
            generated_content TEXT,
            places_data TEXT,
            creation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 思い出テーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            itinerary_id INTEGER NOT NULL,
            caption TEXT,
            photo BLOB,
            creation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (itinerary_id) REFERENCES itineraries (id)
        )
    ''')
    conn.commit()
    conn.close()

# DB初期化を実行
init_db()

# --- 3. 認証関連コードは削除済み ---

# --- 4. Google Maps関連のヘルパー関数 ---
def get_coordinates(address):
    geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "key": GOOGLE_PLACES_API_KEY,
        "language": "ja",
        "region": "JP"
    }
    try:
        response = requests.get(geocode_url, params=params)
        response.raise_for_status()
        results = response.json()
        if results["status"] == "OK" and results["results"]:
            location = results["results"][0]["geometry"]["location"]
            return f"{location['lat']},{location['lng']}"
        else:
            print(f"Geocoding失敗: {results.get('status')}, {results.get('error_message', '')}")
            return None
    except Exception as e:
        print(f"Geocodingエラー: {e}")
        return None

def search_google_places(query: str,
                         location_bias: str = None,
                         place_type: str = "tourist_attraction",
                         min_rating: float = 4.0,
                         price_levels: str = None):
    print("--- Google Places API 呼び出し ---")
    print(f"Query: {query}, Location Bias: {location_bias}, Type: {place_type}, Rating: {min_rating}, Price: {price_levels}")
    base_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "key": GOOGLE_PLACES_API_KEY,
        "language": "ja",
        "region": "JP",
        "type": place_type,
    }
    if location_bias:
        params["location"] = location_bias
        params["radius"] = 20000
    print(f"リクエストパラメータ: {params}")
    try:
        response = requests.get(base_url, params=params)
        response.raise_for_status()
        results = response.json()
        status = results.get("status")
        if status == "OK":
            filtered_places = []
            count = 0
            for place in results.get("results", []):
                place_rating = place.get("rating", 0)
                place_price = place.get("price_level", None)
                if place_rating < min_rating: continue
                if price_levels:
                    try:
                        allowed_levels = [int(x.strip()) for x in price_levels.split(',')]
                        if place_price not in allowed_levels: continue
                    except ValueError: print(f"価格レベルの解析エラー: {price_levels}")
                filtered_places.append({
                    "name": place.get("name"), "address": place.get("formatted_address"),
                    "rating": place_rating, "price_level": place_price,
                    "types": place.get("types", []), "place_id": place.get("place_id"),
                })
                count += 1
                if count >= 5: break
            if not filtered_places:
                return json.dumps({"error": "条件に合致する場所がありませんでした。"}, ensure_ascii=False)
            return json.dumps(filtered_places, ensure_ascii=False)
        else:
            error_msg = results.get('error_message', '')
            print(f"Google Places API エラー: {status}, {error_msg}")
            return json.dumps({"error": f"Google Places API Error: {status}, {error_msg}"}, ensure_ascii=False)
    except Exception as e:
        print(f"HTTPリクエストエラー: {e}")
        return json.dumps({"error": f"HTTPエラー: {e}"}, ensure_ascii=False)


# --- 5. OpenAIのFunction Callingを組み込むための準備 ---

# ★★★ OpenAI v1.x 対応: functions -> tools 形式に変更 ★★★
tools = [
    {
        "type": "function",
        "function": {
            "name": "search_google_places",
            "description": "Google Places APIを使って観光名所やレストランなどを検索する。隠れ家的なお店や、静かなカフェ、旅館など具体的な場所情報が必要なときに呼び出す。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索したい場所のキーワード (例: '京都 抹茶 スイーツ')"},
                    "location_bias": {"type": "string", "description": "検索の中心とする緯度経度 (例: '35.0116,135.7681')。行き先の座標を指定すると精度が上がる。"},
                    "place_type": {
                        "type": "string",
                        "description": "検索する場所の種類",
                        "enum": [
                            "tourist_attraction", "restaurant", "lodging", "cafe",
                            "museum", "park", "art_gallery", "store"
                        ]
                    },
                    "min_rating": {"type": "number", "description": "検索結果に含める最低評価 (例: 4.0)"},
                    "price_levels": {"type": "string", "description": "検索結果に含める価格帯（カンマ区切り、例: '1,2'）。1:安い, 2:普通, 3:やや高い, 4:高い"}
                },
                "required": ["query", "place_type"]
            }
        }
    }
]
# --- ここまで tools 定義 ---

# --- 呼び出し可能な関数名と実際の関数オブジェクトのマッピング (変更なし) ---
available_functions = {
    "search_google_places": search_google_places
}
# --- ここまで 関数マッピング ---

# ★★★ OpenAI v1.x 対応: API呼び出しとレスポンス処理を修正 ★★★
def run_conversation_with_function_calling(messages):
    """
    OpenAIに対しチャットを送信し、もしTool Callがあれば実行して結果を再度OpenAIに渡す。
    最終的に得られたアシスタントからのテキスト返信と、関数が呼ばれた場合はその結果(JSON文字列)を返す。
    """
    try:
        # --- OpenAI API v1.x 呼び出し (1回目) ---
        response = client.chat.completions.create(
            # model="gpt-3.5-turbo-0613", # 古いモデル名
            model="gpt-3.5-turbo",     # 推奨: 最新のgpt-3.5-turboモデル
            messages=messages,
            tools=tools,              # ★ functions -> tools
            tool_choice="auto"        # ★ function_call -> tool_choice
        )
        # --- ここまで OpenAI API呼び出し (1回目) ---
        response_message = response.choices[0].message # ★ レスポンスオブジェクト取得

        # ★ Tool Call が要求されているかチェック (旧 Function Call)
        tool_calls = response_message.tool_calls
        if tool_calls:
            # 最初のTool Callを処理（複数Tool Callには対応しないシンプルな実装）
            tool_call = tool_calls[0]
            function_name = tool_call.function.name
            function_to_call = available_functions.get(function_name)

            if function_to_call:
                # 引数を取得
                function_args = json.loads(tool_call.function.arguments)

                # location_biasの補完ロジック (変更なし)
                if 'location_bias' not in function_args and 'dest' in st.session_state and st.session_state.dest:
                     coords = get_coordinates(st.session_state.dest)
                     if coords:
                         function_args['location_bias'] = coords
                         print(f"座標が見つかりました。location_bias を補完: {coords}")
                     else:
                         print(f"座標が見つかりませんでした。location_bias はなしで検索します。")

                # ★ 実際の関数を実行
                function_response = function_to_call(**function_args)

                # ★★★ Tool Call の結果をメッセージ履歴に追加 ★★★
                messages.append(response_message) # AIの応答（Tool Call指示）を履歴に追加
                messages.append(
                    {
                        "tool_call_id": tool_call.id, # ★ Tool Call ID が必要
                        "role": "tool",             # ★ role は "tool"
                        "name": function_name,
                        "content": function_response, # 関数の実行結果(JSON文字列)
                    }
                )
                # --- ここまで toolロールメッセージ追加 ---

                # 2回目のリクエスト (ツールの結果を考慮した最終応答)
                print("--- Sending tool results back to OpenAI ---") # デバッグ用
                print(f"Messages sent (2nd req): {messages}")      # デバッグ用
                second_response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=messages # ツール結果を含むメッセージ履歴
                )
                # --- ここまで OpenAI API呼び出し (2回目) ---

                # ★ 最終応答と関数結果(JSON文字列)を返す
                final_content = second_response.choices[0].message.content
                return final_content, function_response
            else:
                # 指定された関数が見つからない場合
                print(f"Error: Function '{function_name}' not found in available_functions.")
                return f"エラー: 内部関数 '{function_name}' が見つかりません。", None
        else:
            # --- Tool Call なしの通常返信 ---
            final_content = response_message.content
            return final_content, None
            # --- ここまで 通常返信の場合 ---

    except openai.APIError as e:
        # OpenAI API自体から返されたエラー (例: レート制限、認証エラー)
        st.error(f"OpenAI APIエラーが発生しました: {e}")
        print(f"OpenAI API Error: {e.status_code} - {e.message}") # 詳細ログ
        return "申し訳ありません、AIとの通信中にAPIエラーが発生しました。", None
    except Exception as e:
        # その他の予期せぬエラー
        st.error(f"OpenAIとの通信中に予期せぬエラーが発生しました: {e}")
        import traceback
        st.error(traceback.format_exc()) # 詳細なトレースバックを表示
        return "申し訳ありません、AIとの通信中に予期せぬエラーが発生しました。", None


# --- 6. Streamlitの画面構成 (認証なし) ---

# --- サイドバー ---
st.sidebar.header("メニュー")
menu_choice = st.sidebar.radio("", ["新しい旅を計画する", "過去の旅のしおりを見る"], key="main_menu", label_visibility="collapsed")

# --- セッションステート初期化 ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "itinerary_generated" not in st.session_state:
    st.session_state.itinerary_generated = False
# ...(他のセッションステートも同様)...
if "generated_shiori_content" not in st.session_state:
    st.session_state.generated_shiori_content = None
if "final_places_data" not in st.session_state:
    st.session_state.final_places_data = None
if "basic_info_submitted" not in st.session_state:
    st.session_state.basic_info_submitted = False
if "preferences_submitted" not in st.session_state:
    st.session_state.preferences_submitted = False
if "preferences" not in st.session_state:
    st.session_state.preferences = {}


# --- メインコンテンツ ---

# --- 7. 新しい旅を計画する ---
if menu_choice == "新しい旅を計画する":
    st.header("新しい旅の計画")
    st.subheader("1. 旅の基本情報を入力")

    with st.form("basic_info_form"):
        # フォーム要素 (変更なし)
        st.session_state.dest = st.text_input("行き先 (例: 京都、箱根)", value=st.session_state.get('dest', ''))
        st.session_state.purp = st.text_area("旅の目的や気分", value=st.session_state.get('purp', ''))
        st.session_state.comp = st.selectbox("同行者", ["一人旅", "夫婦・カップル", "友人", "家族"], index=["一人旅", "夫婦・カップル", "友人", "家族"].index(st.session_state.get('comp', '一人旅')))
        st.session_state.days = st.number_input("旅行日数", min_value=1, max_value=30, step=1, value=st.session_state.get('days', 2))
        st.session_state.budg = st.select_slider("予算感", options=["気にしない", "安め", "普通", "高め"], value=st.session_state.get('budg', "普通"))
        submitted_basic = st.form_submit_button("基本情報を確定")

    if submitted_basic:
        if not st.session_state.dest:
            st.warning("行き先を入力してください。")
        else:
            st.success(f"基本情報を受け付けました: {st.session_state.dest}への{st.session_state.comp}旅行 ({st.session_state.days}日間)")
            st.session_state.basic_info_submitted = True
            st.session_state.itinerary_generated = False
            st.session_state.generated_shiori_content = None
            st.session_state.final_places_data = None
            st.session_state.preferences_submitted = False
            st.session_state.preferences = {}

    if st.session_state.basic_info_submitted:
        st.subheader("2. あなたの好みを教えてください")
        with st.form("preferences_form"):
            # 好み入力フォーム要素 (変更なし)
            st.session_state.pref_pace = st.radio("旅のペースは？", ["のんびり", "普通", "アクティブ"], index=["のんびり", "普通", "アクティブ"].index(st.session_state.get('pref_pace', '普通')))
            st.session_state.pref_nature = st.slider("自然(1～5)", 1, 5, st.session_state.get('pref_nature', 3))
            # ...(他のスライダーや選択肢も同様)...
            st.session_state.pref_culture = st.slider("歴史文化(1～5)", 1, 5, st.session_state.get('pref_culture', 3))
            st.session_state.pref_art = st.slider("アート(1～5)", 1, 5, st.session_state.get('pref_art', 3))
            st.session_state.pref_food_local = st.radio("食事スタイル", ["地元の人気店", "隠れ家的なお店", "こだわらない"], index=["地元の人気店", "隠れ家的なお店", "こだわらない"].index(st.session_state.get('pref_food_local', '地元の人気店')))
            st.session_state.pref_food_style = st.multiselect("好きな料理", ["和食", "洋食", "カフェ", "スイーツ", "居酒屋"], default=st.session_state.get('pref_food_style', []))
            st.session_state.pref_accom_type = st.radio("宿タイプ", ["ホテル", "旅館", "民宿・ゲストハウス", "こだわらない"], index=["ホテル", "旅館", "民宿・ゲストハウス", "こだわらない"].index(st.session_state.get('pref_accom_type', 'ホテル')))
            st.session_state.pref_accom_view = st.checkbox("宿の景色重視", value=st.session_state.get('pref_accom_view', False))
            st.session_state.pref_vibe_quiet = st.radio("好み雰囲気", ["静かで落ち着いた", "活気のある"], index=["静かで落ち着いた", "活気のある"].index(st.session_state.get('pref_vibe_quiet', '静かで落ち着いた')))
            st.session_state.pref_vibe_discover = st.checkbox("隠れた発見をしたい", value=st.session_state.get('pref_vibe_discover', True))
            st.session_state.pref_experience = st.multiselect("興味ある体験", ["温泉", "ものづくり", "寺社仏閣", "食べ歩き", "ショッピング", "何もしない"], default=st.session_state.get('pref_experience', []))
            submitted_prefs = st.form_submit_button("好みを確定して旅のしおりを生成")

        if submitted_prefs:
            st.session_state.preferences_submitted = True
            st.session_state.preferences = {
                "pace": st.session_state.pref_pace, "nature": st.session_state.pref_nature,
                "culture": st.session_state.pref_culture, "art": st.session_state.pref_art,
                "food_local": st.session_state.pref_food_local, "food_style": st.session_state.pref_food_style,
                "accom_type": st.session_state.pref_accom_type, "accom_view": st.session_state.pref_accom_view,
                "vibe_quiet": st.session_state.pref_vibe_quiet, "vibe_discover": st.session_state.pref_vibe_discover,
                "experience": st.session_state.pref_experience
            }
            st.info("しおりを作成中です。少々お待ちください...")
            prompt = f"""
あなたは旅のプランナー「Okosy」です。ユーザーの入力情報をもとに、SNS映えや定番から少し離れた、ユーザー自身の感性に寄り添うような、パーソナルな旅のしおりを作成してください。

【基本情報】
- 行き先: {st.session_state.dest}
- 目的・気分: {st.session_state.purp}
- 同行者: {st.session_state.comp}
- 旅行日数: {st.session_state.days}日
- 予算感: {st.session_state.budg}

【ユーザーの好み】
{json.dumps(st.session_state.preferences, ensure_ascii=False, indent=2)}

【出力指示】
1.  **構成:** {st.session_state.days}日間の旅程を、各日ごとに「午前」「午後」「夜」のセクションに分けて提案してください。
2.  **内容:**
    * なぜその場所や過ごし方がユーザーの目的・気分・好みに合っているか、**感性的な言葉**で理由や提案コメントを添えてください。
    * ユーザーの「隠れた発見をしたい」という気持ち（`vibe_discover`がTrueの場合）を考慮し、定番すぎないスポットや体験も提案に含めてください。
    * 食事や宿泊の好みも反映してください。
    * `search_google_places`ツールを**必要に応じて**呼び出し、具体的な場所の候補を検索してください。
    * ツールの結果が得られた場合は、その場所名を旅程に自然に組み込んでください。エラーが返ってきた場合は、代替案を提示してください。
3.  **形式:** 全体を読みやすい**マークダウン形式**で出力してください。

Okosyとして、ユーザーに最高の旅体験をデザインしてください。
            """
            st.session_state.messages = [{"role": "user", "content": prompt}]
            with st.spinner("AIが旅のしおりを作成しています..."):
                # ★★★ run_conversation_with_function_calling を呼び出す ★★★
                final_response, places_api_result = run_conversation_with_function_calling(st.session_state.messages)
            if final_response:
                st.session_state.itinerary_generated = True
                st.session_state.generated_shiori_content = final_response
                st.session_state.final_places_data = places_api_result
                st.success("旅のしおりが完成しました！")
            else:
                st.error("しおりの生成中にエラーが発生しました。")

    if st.session_state.itinerary_generated and st.session_state.generated_shiori_content:
        st.subheader("あなたの旅のしおり")
        st.markdown(st.session_state.generated_shiori_content)
        st.markdown("---")
        if st.session_state.final_places_data:
            with st.expander("提案に含まれる可能性のある場所リスト (Google Places APIの結果)"):
                # (場所リスト表示部分は変更なし)
                try:
                    places = json.loads(st.session_state.final_places_data)
                    if isinstance(places, list):
                        try:
                            df = pd.DataFrame(places)
                            st.dataframe(df)
                        except Exception as e: st.write(places)
                    elif isinstance(places, dict) and 'error' in places:
                        st.warning(f"場所情報の取得中にエラーが発生しました: {places['error']}")
                    else: st.write(places)
                except json.JSONDecodeError:
                    st.error("場所情報の解析中にエラーが発生しました。")
                    st.text(st.session_state.final_places_data)
                except Exception as e:
                     st.error(f"場所情報の表示中に予期せぬエラーが発生しました: {e}")
                     st.text(st.session_state.final_places_data)

        st.subheader("しおりを保存しますか？")
        st.session_state.shiori_name_input = st.text_input("しおりの名前", value=st.session_state.get('shiori_name_input', f"{st.session_state.get('dest', '旅行')}の旅 {datetime.date.today()}"))
        if st.button("このしおりを保存する", key="save_shiori"):
            shiori_name = st.session_state.shiori_name_input
            if not shiori_name:
                st.warning("しおりの名前を入力してください。")
            else:
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO itineraries (name, preferences, generated_content, places_data) VALUES (?, ?, ?, ?)",
                        (shiori_name, json.dumps(st.session_state.preferences, ensure_ascii=False),
                         st.session_state.generated_shiori_content, st.session_state.final_places_data)
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"しおり「{shiori_name}」を保存しました！")
                    # 状態リセット (変更なし)
                    keys_to_reset = [
                        "basic_info_submitted", "preferences_submitted", "itinerary_generated",
                        "generated_shiori_content", "final_places_data", "preferences",
                        "dest", "purp", "comp", "days", "budg", "pref_pace", "pref_nature",
                        "pref_culture", "pref_art", "pref_food_local", "pref_food_style",
                        "pref_accom_type", "pref_accom_view", "pref_vibe_quiet",
                        "pref_vibe_discover", "pref_experience", "shiori_name_input"
                    ]
                    for key in keys_to_reset:
                        if key in st.session_state: del st.session_state[key]
                    st.rerun()
                except Exception as e:
                    st.error(f"しおりの保存中にエラーが発生しました: {e}")
                    import traceback
                    st.error(traceback.format_exc())

# --- 8. 過去の旅のしおりを見る ---
elif menu_choice == "過去の旅のしおりを見る":
    st.header("過去の旅のしおり")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, creation_date, preferences, generated_content, places_data FROM itineraries ORDER BY creation_date DESC")
        past_itineraries = cursor.fetchall()

        if not past_itineraries:
            st.info("保存されているしおりはありません。")
        else:
            if 'selected_itinerary_id' not in st.session_state:
                st.session_state.selected_itinerary_id = None

            itinerary_options = {f"{row[1]} ({row[2].split()[0]})": row[0] for row in past_itineraries}
            if itinerary_options:
                 selected_name = st.selectbox("表示するしおりを選択してください", options=list(itinerary_options.keys()), index=0)
                 st.session_state.selected_itinerary_id = itinerary_options.get(selected_name)

            selected_itinerary = None
            if st.session_state.selected_itinerary_id is not None:
                for row in past_itineraries:
                    if row[0] == st.session_state.selected_itinerary_id:
                        selected_itinerary = row
                        break

            if selected_itinerary:
                iti_id, iti_name, iti_date, iti_prefs_json, iti_content, iti_places_json = selected_itinerary
                st.subheader(f"しおり: {iti_name}")
                st.caption(f"作成日: {iti_date}")
                st.markdown("---")
                st.markdown(iti_content)
                st.markdown("---")

                with st.expander("このしおりを作成した時の好み"):
                    # (好み表示部分は変更なし)
                    try:
                        prefs_dict = json.loads(iti_prefs_json)
                        st.json(prefs_dict)
                    except Exception as e: st.text(iti_prefs_json) # エラー時はテキスト表示

                if iti_places_json:
                    with st.expander("関連する場所の情報 (Google Places APIの結果)"):
                         # (場所情報表示部分は変更なし)
                        try:
                            places_data = json.loads(iti_places_json)
                            if isinstance(places_data, list):
                                 try:
                                    df = pd.DataFrame(places_data)
                                    st.dataframe(df)
                                 except Exception as e: st.write(places_data)
                            elif isinstance(places_data, dict) and 'error' in places_data:
                                st.warning(f"場所情報の取得時にエラーが記録されています: {places_data['error']}")
                            else: st.write(places_data)
                        except json.JSONDecodeError: st.text(iti_places_json) # エラー時はテキスト表示
                        except Exception as e: st.text(iti_places_json) # エラー時はテキスト表示

                st.subheader("旅の思い出")
                with st.form("memory_form"):
                    # (思い出フォーム部分は変更なし)
                    memory_caption = st.text_area("キャプション", key=f"mem_caption_{iti_id}")
                    uploaded_photo = st.file_uploader("写真を選択", type=["jpg", "jpeg", "png"], key=f"mem_photo_{iti_id}")
                    submitted_memory = st.form_submit_button("思い出を追加")
                    if submitted_memory:
                        if uploaded_photo is not None:
                            photo_bytes = uploaded_photo.getvalue()
                            try:
                                conn_mem = get_db_connection()
                                cursor_mem = conn_mem.cursor()
                                cursor_mem.execute("INSERT INTO memories (itinerary_id, caption, photo) VALUES (?, ?, ?)",
                                    (iti_id, memory_caption, photo_bytes))
                                conn_mem.commit()
                                conn_mem.close()
                                st.success("思い出を追加しました！")
                                st.rerun()
                            except Exception as e:
                                st.error(f"思い出の保存中にエラーが発生しました: {e}")
                                import traceback
                                st.error(traceback.format_exc())
                        else: st.warning("写真を選択してください。")

                cursor.execute("SELECT id, caption, photo, creation_date FROM memories WHERE itinerary_id = ? ORDER BY creation_date DESC", (iti_id,))
                past_memories = cursor.fetchall()
                if past_memories:
                    st.write("---")
                    st.write("**登録済みの思い出:**")
                    for mem_id, mem_caption, mem_photo_blob, mem_date in past_memories:
                        # (思い出表示部分は変更なし)
                        cols = st.columns([1, 3])
                        with cols[0]:
                            try:
                                image = Image.open(io.BytesIO(mem_photo_blob))
                                st.image(image, width=150)
                            except Exception as e: st.warning(f"画像表示エラー: {e}")
                        with cols[1]:
                            st.write(f"**{mem_date.split()[0]}**")
                            st.write(mem_caption if mem_caption else "(キャプションなし)")
                            if st.button("削除", key=f"delete_mem_{mem_id}"):
                                try:
                                    conn_del = get_db_connection()
                                    cursor_del = conn_del.cursor()
                                    cursor_del.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
                                    conn_del.commit()
                                    conn_del.close()
                                    st.success("思い出を削除しました。")
                                    st.rerun()
                                except Exception as e: st.error(f"削除中にエラー: {e}")
                        st.write("---")
                else: st.info("このしおりにはまだ思い出が登録されていません。")

        conn.close() # DB接続を閉じる

    except Exception as e:
        st.error(f"過去のしおりの読み込み中にエラーが発生しました: {e}")
        import traceback
        st.error(traceback.format_exc())
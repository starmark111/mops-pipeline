import json
import os
from datetime import datetime, timedelta
import calendar
import requests

# 初始設定
CONFIG_FILE = "/Users/starmark/Downloads/scripts/rotation_state.json"
DEFAULT_ROTATION = ["黃同學", "劉同學", "王同學"]

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"rotation": DEFAULT_ROTATION, "next_index": 1}

def save_config(rotation, next_index):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"rotation": rotation, "next_index": next_index}, f, ensure_ascii=False, indent=4)

def get_monday(date):
    return date - timedelta(days=date.weekday())

def generate_schedule(manual_start_name=None, new_rotation=None, weeks=4):
    # 1. 載入紀錄
    config = load_config()
    rotation = new_rotation if new_rotation else config["rotation"]

    # 2. 確定起始索引
    if manual_start_name and manual_start_name in rotation:
        person_index = rotation.index(manual_start_name)
    else:
        person_index = config["next_index"] % len(rotation)

    # 時間計算邏輯
    today = datetime.today()
    start_monday = get_monday(today)

    result = []
    monday = start_monday

    # 3. 產生排程（只產生指定週數）
    current_idx = person_index
    final_next_index = person_index  # 先初始化，保證一定會有值

    for week in range(weeks):
        sunday = monday + timedelta(days=6)
        person = rotation[current_idx]
        result.append(f"{monday.strftime('%m/%d')} ～ {sunday.strftime('%m/%d')}　{person}")

        # 計算下一週的起始人員索引
        next_idx = (current_idx + 1) % len(rotation)

        # 記錄「下一次執行」應從誰開始
        if week == 0:
            final_next_index = next_idx

        current_idx = next_idx
        monday += timedelta(days=7)

    # 4. 儲存進度
    save_config(rotation, final_next_index)

    return result

def send_to_line(message_text):
    """發送訊息到 LINE Bot"""
    line_token = "2rZZ4ZNElqU/Py8f0AX7ObFsOxQCgHPViad4szhIK5uT59uDTFpR3OprhLRvVMxjvkriekquktIVhCj8PkwQqUmdt9fbBuwsvnpi46A+TvUWMeqOVDOb2748AnmCL1YcSb1nm6CP2NDboTTBeTFBbwdB04t89/1O/w1cDnyilFU="
    user_id = "Ua1b88020d3f6a8587a77ae6e0062c45e"

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {line_token}"
    }

    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": message_text
            }
        ]
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            print("✅ LINE 訊息已發送")
        else:
            print(f"❌ LINE 發送失敗: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ LINE 發送錯誤: {str(e)}")

if __name__ == "__main__":
    try:
        # 範例：若要指定某人起始，請解除下面註解並填入名字
        # schedule = generate_schedule(manual_start_name="王同學", weeks=4)

        # 範例：若有人員變動，直接傳入新名單
        # new_list = ["黃同學", "王同學", "新同學"]
        # schedule = generate_schedule(new_rotation=new_list, weeks=4)

        schedule = generate_schedule(weeks=4)

        message = "=== 值日生輪值表 ===\n"
        for item in schedule:
            message += item + "\n"

        # 打印到終端（給 n8n 看）
        print(message.strip())

        # 發送到 LINE
        send_to_line(message.strip())

    except Exception as e:
        error_msg = f"❌ 錯誤：{str(e)}"
        print(error_msg)
        send_to_line(error_msg)
        import traceback
        print(traceback.format_exc())

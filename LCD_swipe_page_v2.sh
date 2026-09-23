"""
執行方式，用下面這指令執行bash就行，這不是python
bash /Users/starmark/Downloads/scripts/LCD_swipe_page_v2.sh
"""


#!/bin/bash

# 參數初始化
width=""
height=""
device_type=""
device_model_name=""
device_serial_number=""
interval=0.5 # 預設翻頁間隔

function print_device_info() {
    echo "-------------------------------"
    echo "機器型號: $device_model_name"
    echo "機器款式: $device_type"
    echo "機器序號: $device_serial_number"
    echo "螢幕尺寸: $output (W:$width, H:$height)"
    echo "翻頁間隔: $interval 秒"
    echo "-------------------------------"
}

function get_device_type() {
    device_model_name=$(adb shell getprop ro.product.model | tr -d '\r')
    if [[ "$device_model_name" == "Lenovo TB-X306F" || "$device_model_name" == "SM-T500" || "$device_model_name" == "SM-P613" ]]; then
        device_type="Pad"
    elif [[ "$device_model_name" == "Pixel 3a" || "$device_model_name" == "Pixel 6a" ]]; then
        device_type="Phone"
    elif [[ "$device_model_name" == "BNRV1000" || "$device_model_name" == "BNRV1100" || "$device_model_name" == "BNRV1300" ]]; then
        device_type="EPD"
    else
        device_type="未知型號"
    fi
}

function set_screen_size_and_orientation() {
    adb shell settings put system accelerometer_rotation 0
    echo "請選擇螢幕方向:"
    echo "1. 直向 (Portrait)"
    echo "2. 橫向 (Landscape)"
    read -p "請輸入 (1/2): " choice

    case $choice in
        1) adb shell settings put system user_rotation 0 ;;
        2) adb shell settings put system user_rotation 1 ;;
        *) echo "無效輸入，預設直向"; adb shell settings put system user_rotation 0 ;;
    esac

    output=$(adb shell wm size | awk '{print $3}' | tr -d '\r')
    device_serial_number=$(adb devices | awk 'NR==2 {print $1}')
    orientation=$(adb shell settings get system user_rotation | tr -d '\r')

    if [ "$orientation" -eq 1 ] || [ "$orientation" -eq 3 ]; then
        width=$(echo "$output" | cut -d 'x' -f 2)
        height=$(echo "$output" | cut -d 'x' -f 1)
        echo "目前設定：橫向模式"
    else
        width=$(echo "$output" | cut -d 'x' -f 1)
        height=$(echo "$output" | cut -d 'x' -f 2)
        echo "目前設定：直向模式"
    fi
}

# --- 動作 Function ---
function swipe_right_to_left() {
    start_x=$((width * 80 / 100)); start_y=$((height / 2))
    end_x=$((width * 40 / 100)); end_y=$((height / 2))
    adb shell input swipe "$start_x" "$start_y" "$end_x" "$end_y" 150
}

function swipe_left_to_right() {
    start_x=$((width * 10 / 100)); start_y=$((height / 2))
    end_x=$((width * 50 / 100)); end_y=$((height / 2))
    adb shell input swipe "$start_x" "$start_y" "$end_x" "$end_y" 150
}

function tap_center() {
    X=$((width / 2)); Y=$((height / 2))
    adb shell input tap $X $Y
}

# --- 測試模式 ---
function run_iteration() {
    # 詢問翻頁次數
    read -p "請輸入要翻幾頁 (預設 100): " k_count
    k_count=${k_count:-100}

    echo ">>> 開始執行單次測試 (共 $k_count 次翻頁)"

    # 只保留一圈翻頁迴圈
    for ((k=1; k<=k_count; k++))
    do
        printf "進度: %d/%d\r" $k $k_count
        swipe_right_to_left
        # 加上您選定的間隔時間
        sleep $interval
    done

    echo -e "\n>>> 測試完成"
    sleep 1
}

function tear_down() {
    echo "恢復系統預設值..."
    adb shell settings put system accelerometer_rotation 1
    adb shell settings put system user_rotation 0
}

# --- 主程式進入點 ---
get_device_type
set_screen_size_and_orientation

# 設定間隔時間
read -p "請設定操作間隔秒數 (例如 0.5 或 1): " input_interval
interval=${input_interval:-0.5}

print_device_info

# 功能選單
while true; do
    echo "======== 請選擇執行功能 ========"
    echo "1. 執行翻頁壓力測試 (Iteration)"
    echo "2. 回到書本開頭 (Scrobber to Beginning)"
    echo "3. 點擊螢幕中心 (Tap Center)"
    echo "4. 恢復系統設定並退出 (Tear Down & Exit)"
    echo "================================"
    read -p "請選擇功能編號: " menu_choice

    case $menu_choice in
        1) run_iteration ;;
        2)
            # 這裡引用您原有的 scrobber 邏輯
            X=$((width * 5 / 100)); Y=$((height * 91 / 100))
            adb shell input tap "$X" "$Y"
            echo "已執行回到開頭"
            ;;
        3) tap_center ;;
        4) tear_down; break ;;
        *) echo "無效選擇，請重新輸入" ;;
    esac
done

echo "======== 測試結束 ========"


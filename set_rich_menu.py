import os
import requests
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    RichMenuRequest,
    RichMenuArea,
    RichMenuSize,
    RichMenuBounds,
    MessageAction
)
from dotenv import load_dotenv

# Load credentials
load_dotenv()
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

if not CHANNEL_ACCESS_TOKEN:
    print("Error: LINE_CHANNEL_ACCESS_TOKEN not found in .env")
    exit(1)

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

def setup_rich_menu():
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 1. Delete existing rich menus
        rich_menu_list = line_bot_api.get_rich_menu_list()
        for menu in rich_menu_list.richmenus:
            print(f"Deleting existing rich menu: {menu.rich_menu_id}")
            line_bot_api.delete_rich_menu(menu.rich_menu_id)

        # 2. Define Rich Menu (3x2 grid, 1200x810)
        width = 1200
        height = 810
        grid_w = width // 3
        grid_h = height // 2

        rich_menu_request = RichMenuRequest(
            size=RichMenuSize(width=width, height=height),
            selected=True,
            name="Official Pickup Menu",
            chat_bar_text="點我開啟接送選單",
            areas=[
                # Row 1, Col 1: 已到達校門
                RichMenuArea(
                    bounds=RichMenuBounds(x=0, y=0, width=grid_w, height=grid_h),
                    action=MessageAction(label="已到達校門", text="已到達校門")
                ),
                # Row 1, Col 2: 即將到達
                RichMenuArea(
                    bounds=RichMenuBounds(x=grid_w, y=0, width=grid_w, height=grid_h),
                    action=MessageAction(label="即將到達", text="即將到達")
                ),
                # Row 1, Col 3: 會晚點到
                RichMenuArea(
                    bounds=RichMenuBounds(x=grid_w*2, y=0, width=grid_w, height=grid_h),
                    action=MessageAction(label="會晚點到", text="會晚點到")
                ),
                # Row 2, Col 1: 身份註冊
                RichMenuArea(
                    bounds=RichMenuBounds(x=0, y=grid_h, width=grid_w, height=grid_h),
                    action=MessageAction(label="身份註冊", text="身份註冊")
                ),
                # Row 2, Col 2: 聯絡學校
                RichMenuArea(
                    bounds=RichMenuBounds(x=grid_w, y=grid_h, width=grid_w, height=grid_h),
                    action=MessageAction(label="聯絡學校", text="聯絡學校")
                ),
                # Row 2, Col 3: 接到孩子
                RichMenuArea(
                    bounds=RichMenuBounds(x=grid_w*2, y=grid_h, width=grid_w, height=grid_h),
                    action=MessageAction(label="接到孩子", text="接到孩子")
                )
            ]
        )

        # 3. Create Rich Menu
        rich_menu_id = line_bot_api.create_rich_menu(rich_menu_request).rich_menu_id
        print(f"Successfully created rich menu: {rich_menu_id}")

        # 4. Upload Image using requests (to bypass API client binary issue)
        image_path = "rich_menu_1200x810.png"
        if not os.path.exists(image_path):
             image_path = "rich_menu_800x540.png"
             if not os.path.exists(image_path):
                  print("Error: Could not find rich_menu_1200x810.png or rich_menu_800x540.png")
                  return

        headers = {
            'Authorization': f'Bearer {CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'image/png'
        }
        
        with open(image_path, 'rb') as f:
            url = f'https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content'
            response = requests.post(url, headers=headers, data=f)
            
        if response.status_code == 200:
            print(f"Successfully uploaded image: {image_path}")
        else:
            print(f"Failed to upload image: {response.status_code} - {response.text}")
            return

        # 5. Set as default
        line_bot_api.set_default_rich_menu(rich_menu_id)
        print("Successfully set rich menu as default for all users!")

if __name__ == "__main__":
    setup_rich_menu()

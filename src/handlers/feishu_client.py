import os
import lark_oapi as lark

APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

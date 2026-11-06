# 漫剧工作室 · 内置 AI 漫剧能力包
# 来源：WorkBuddy 技能市场的 ai-manga-drama / dynamic-comic-video 等 skill
#  - drama_video : 动态漫剧视频合成（分镜图 + 推镜/呼吸/速度线/抖动 → mp4）
#  - manga       : 条漫长图拼接（带对白气泡 / 旁白框）
#  - cloud       : AI Hive 云端视频生成（文生/图生/参考生视频，需 sk-api-* 密钥）
from . import drama_video, manga, cloud  # noqa: F401

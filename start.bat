@echo off
chcp 936 >nul
title DeepSeek QQ 机器人启动器

echo ============================================
echo   启动 DeepSeek 大肥鱼 QQ 机器人
echo ============================================
echo.

echo [1/2] 启动 NapCat（QQ 协议端）...
start "NapCat" /D "C:\Users\admin\Downloads\NapCat" cmd /c "node.exe index.js -q 3635153756"

echo [2/2] 启动本项目（main.py）...
start "DeepSeekQQ" /D "C:\Users\admin\WorkBuddy\DeepSeek接入QQ" cmd /c "C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe main.py"

echo.
echo 完成！两个窗口已打开，保持它们开着即可。
echo  - NapCat 窗口：QQ 协议端（登录小号 3635153756）
echo  - DeepSeekQQ 窗口：业务处理（日志会显示"协议端已连入"）
echo.
echo 停止方式：关闭对应窗口即可。
echo.
pause

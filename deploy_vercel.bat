@echo off
title Deploy AI Gmail Job Hunter to Vercel
cd /d "%~dp0"
echo ======================================================================
echo Deploying AI Gmail Job Hunter Agent to Vercel...
echo ======================================================================
python deploy_vercel.py
pause

$ErrorActionPreference = 'Stop'
$studyRoot = $PSScriptRoot
$studyPython = Join-Path $studyRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $studyPython)) {
    python -m venv (Join-Path $studyRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建独立虚拟环境失败。' }
}
& $studyPython -m pip install --disable-pip-version-check -r (Join-Path $studyRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败，请检查网络。' }
$env:SE_CACHE_PATH = Join-Path $studyRoot '.cache\selenium'
$env:SE_AVOID_STATS = 'true'
& $studyPython -c 'from selenium.webdriver.common.selenium_manager import SeleniumManager; import json; print(json.dumps(SeleniumManager().binary_paths(["--browser", "edge"]), ensure_ascii=False))'
if ($LASTEXITCODE -ne 0) { throw 'Edge 驱动准备失败，请检查网络或在 drivers 文件夹放入匹配版本的 msedgedriver.exe。' }
Write-Host '独立环境准备完成。请先运行“检查课程.cmd”。'

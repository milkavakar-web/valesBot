# Сборка одного исполняемого файла с ботом и панелью: pyinstaller valesbot.spec
# Результат — dist/ValesBot (на Windows dist/ValesBot.exe). Python ставить не нужно.
# Собирать нужно на той системе, для которой файл: exe — на Windows, и т. д.
# Это делает GitHub Actions (.github/workflows/build.yml) для Windows, macOS и Linux.
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["app/launcher.py"],
    pathex=["."],
    datas=[("app/static", "app/static"), ("app/signals_examples", "app/signals_examples"),
           (".env.example", ".")],
    # uvicorn подгружает цикл событий и протоколы по имени — явно включаем их в сборку.
    # Модули стандартной библиотеки — для сигналов-скриптов: сама программа их не импортирует,
    # и без этого их не было бы в exe.
    hiddenimports=collect_submodules("uvicorn") + ["app.main"] + [
        "statistics", "datetime", "decimal", "fractions", "bisect", "heapq", "collections",
        "itertools", "functools", "math", "random", "json", "re", "zoneinfo"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="ValesBot",
    console=True,  # окно с журналом: закрыть окно — остановить бота
    upx=False,
)

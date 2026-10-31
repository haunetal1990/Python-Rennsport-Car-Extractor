![alt text](https://i.postimg.cc/PxK9gGQC/samplebmwrennsport.jpg "Screenshot Blender")

# Python Rennsport Car Extractor

Extract cars from **Rennsport** and get ready-to-use **Blender** files – meshes, materials, textures and all parts in place.

> ⚠️ **Only works with the game version before November 2025.**
>
> You can download this version via the Steam console:
> 1. Press `Win + R` and enter `steam://open/console`
> 2. In the Steam console, run:
>    ```
>    download_depot 2077750 2077751 8977428734768596951
>    ```

## Requirements

- Windows, Python 3.8+ (no extra packages needed)
- Blender 4.0
- The game's **AES key**

## 1. Unpack the game

```
python rennsport_unpak.py "C:\Path\To\Rennsport-Windows.pak"
```

On the first start you are asked for the **AES key**. It is saved to `rennsport_unpak.ini` next to the script and used automatically from then on (`--reset-key` to change it).

## 2. Create a Blender file

```
python rennsport_blender.py BMW_M4_GT3 -o Export
```

On the first start you are asked for the unpacked `Content` folder and `blender.exe` (saved to `rennsport_blender.ini`).<br />
Result: `Export\BMW_M4_GT3.blend` + textures.

`--list` shows all cars · `--pack` packs textures into the .blend · `--damage` adds damage parts

## Notes

- AES key, Oodle DLL and game files are **not** included.
- Praga Bohema is unfinished in the game and can't be fully assembled.
- Not affiliated with the developers of Rennsport. Use only with a legally owned copy of the game.

---

## Topics
https://forum.studio-397.com/index.php?threads/blender-plugins-from-me.73647/

# TamaExtractor — Reversing del sprite pack del Tamagotchi Smart

📖 [English](README.md) · **Español**

Writeup y herramienta para entender cómo se almacenan los gráficos (sprites) en el
firmware del **Tamagotchi Smart** (2022), y cómo extraerlos a PNG a partir de un
volcado de tu propia flash.

> ⚠️ **Trae tu propio dump.** Este repo contiene **código y documentación**, no
> firmware ni sprites de Bandai. Las imágenes y la ROM son propiedad intelectual de
> Bandai/WiZ; aquí solo se explica cómo trabajar con un volcado que extraigas tú
> mismo de tu propio dispositivo. No subas ni redistribuyas el `.bin` ni los assets.

---

## Índice

- [El dispositivo](#el-dispositivo)
- [TL;DR del formato](#tldr-del-formato)
- [Cómo lo hice (metodología)](#cómo-lo-hice-metodología)
- [El formato del sprite pack en detalle](#el-formato-del-sprite-pack-en-detalle)
- [Uso del extractor](#uso-del-extractor)
- [Créditos](#créditos)
- [Qué hay y qué no hay en este repo](#qué-hay-y-qué-no-hay-en-este-repo)
- [Licencia](#licencia)

---

## El dispositivo

- **Modelo:** Tamagotchi Smart (el de pantalla a color, 2022).
- **Flash:** `MX25L12835F`, una SPI NOR de 16 MB (128 Mbit). El firmware completo
  cabe ahí, y la mayor parte del espacio son datos (gráficos, texto, animaciones),
  no código.
- **Volcado:** un único `.bin` de 16 MB leído de la flash.

## TL;DR del formato

Si solo quieres las constantes mágicas:

| Región | Offset | Contenido |
|---|---|---|
| **Data Pack** | `0x6CE000` | Metadatos, strings, definiciones |
| **Sprite Pack** | `0x730000` | Los gráficos de verdad |

- Los píxeles **no** son color directo: son **índices a una paleta**.
- La paleta usa color **ARGB1555** (16 bits por color), **no** RGB565.

```
ARGB1555  (2 bytes por color, little-endian)
 bit 15 : alpha / transparencia  (1 = transparente, 0 = opaco)
 bits 14-10 : rojo   (5 bits)
 bits  9- 5 : verde  (5 bits)
 bits  4- 0 : azul   (5 bits)
```

El escalado de 5→8 bits es un `<< 3` simple (máximo 248, no 255), igual que hace
smartypants. Un blanco puro sale como `(248, 248, 248)`.

Estructura del sprite pack:

```
0x730000  ┌──────────────────────────┐
          │ header (16 bytes)        │  4× u32 → offsets a las subtablas
          ├──────────────────────────┤
          │ image_defs (6 bytes c/u) │  enlaza cada sprite con su paleta
          ├──────────────────────────┤
          │ sprite_defs (8 bytes c/u)│  ancho, alto, bpp, offset a píxeles
          ├──────────────────────────┤
          │ paletas (ARGB1555)       │  aquí vive el COLOR
          ├──────────────────────────┤
          │ pixel_data (bits packed) │  índices a paleta, empaquetados
          └──────────────────────────┘
```

> Los **offsets del header son relativos al inicio del sprite pack**, no al inicio
> del firmware. El layout exacto de cada `*_def` está documentado abajo; la
> referencia canónica sigue siendo el código de **smartypants** (ver
> [Créditos](#créditos)).

## Cómo lo hice (metodología)

Lo cuento como receta porque sirve para reversear casi cualquier formato gráfico
desconocido, no solo este.

### 1. Reconocimiento

Hashear el dump (para tener referencia), mirar el tamaño y hacer un **análisis de
entropía** con `binwalk` o `ent`. Esto separa a ojo las zonas: el código tiene una
"textura" distinta a los datos estructurados, y lo comprimido/cifrado da entropía
cercana a 1. Un `strings` rápido revela dónde hay texto.

### 2. Triaje visual (el truco estrella)

Abrir el `.bin` como si fuera un bitmap en bruto e ir deslizando el **ancho** hasta
que los sprites *encajan* y aparecen de golpe. Sirve cualquier visor de raw pixels
(rawpixels.net, el import raw de GIMP, o un script de 20 líneas que pinte los bytes
en escala de grises). Esto da el offset aproximado de los gráficos y una pista del
stride.

### 3. Adivinar el formato

Con sprites ya visibles, deducir los **bits por píxel**: ¿1bpp mono, indexado de
2/4/8 bits, o color directo? En pantallas a color casi siempre es indexado + paleta
para ahorrar espacio. Luego localizar la paleta: una tira de valores de 2 bytes
cerca de los gráficos. Decodificar candidatos y probar hasta que los colores tengan
sentido. (Aquí fue donde descubrí que era **ARGB1555** y no RGB565, que era mi
primera hipótesis equivocada.)

### 4. Reversear las tablas

El firmware no tiene píxeles "sueltos": tiene arrays de descriptores (ancho, alto,
offset, índice de paleta). El método de **diffing** es oro: identificas dos sprites
que reconoces, miras en qué se diferencian sus descriptores y deduces qué campo es
qué.

### 5. Escribir el parser incrementalmente

Hardcodear el offset base, leer el header, imprimir lo que crees que son punteros y
**validarlos**: ¿apuntan dentro del archivo?, ¿están alineados?, ¿son crecientes?
Construir el camino mínimo de "renderiza UN sprite", mirarlo a ojo y iterar.

### 6. Validar

Renderizar **todos** los sprites a una hoja de contactos y buscar basura visual
(ancho mal, bpp mal, paleta cruzada) para corregir.

## El formato del sprite pack en detalle

### Header (16 bytes, en el offset base del pack)

Cuatro `u32` little-endian, todos **relativos al inicio del pack**:

| Offset | Campo | Apunta a |
|---|---|---|
| `0`  | `image_defs_offset`  | tabla de image defs |
| `4`  | `sprite_defs_offset`  | tabla de sprite defs |
| `8`  | `palettes_offset`  | array de colores |
| `12` | `pixel_data_offset` | datos de píxeles |

Cada sección va desde su offset hasta el inicio de la siguiente (la de píxeles
llega hasta el final del pack).

### Color: ARGB1555

Cada color son 2 bytes LE. El decodificador es pura aritmética de bits:

```python
def color_from_word(word: int) -> tuple[int, int, int, int]:
    r = (word & 0x7C00) >> 7   # == ((word >> 10) & 0x1F) << 3
    g = (word & 0x03E0) >> 2   # == ((word >>  5) & 0x1F) << 3
    b = (word & 0x001F) << 3   # ==  (word        & 0x1F) << 3
    a = 0 if (word >> 15) else 255   # bit 15: 1 = transparente
    return (r, g, b, a)
```

### sprite_def (8 bytes cada uno)

| Offset | Tipo | Campo |
|---|---|---|
| `0` | `u16` | `pixel_data_index` |
| `2` | `i16` | `offset_x` |
| `4` | `i16` | `offset_y` |
| `6` | `u16` | `props` (bitfield) |

El `props` empaqueta varias cosas. Lo que importa en el Smart:

```
bits 0-1  : índice de bpp   -> [2, 4, 6, 8] bits por píxel
bits 4-5  : índice de ancho -> [8, 16, 32, 64] px
bits 6-7  : índice de alto  -> [8, 16, 32, 64] px
bit  15   : is_quadrupled    (si está, ancho y alto ×4)
```

(Los bits 2-3, 8-13 y 14 existen pero no se usan en el Smart.)

**Gotcha clave:** `pixel_data_index` **no es un offset en bytes**. Es un índice en
unidades del tamaño del propio sprite. El offset real es:

```
byte_count = ancho * alto * bpp / 8
offset_real = pixel_data_index * byte_count
```

### Desempaquetado de píxeles

Los píxeles son índices a paleta empaquetados en bits, **MSB primero** dentro de
cada byte y MSB primero entre los bits de un píxel:

```python
def unpack_indices(data, start, num_pixels, bpp):
    indices, bitpos = [], start * 8
    for _ in range(num_pixels):
        val = 0
        for _ in range(bpp):
            byte = data[bitpos >> 3]
            bit = (byte >> (7 - (bitpos & 7))) & 1
            val = (val << 1) | bit
            bitpos += 1
        indices.append(val)
    return indices
```

### image_def (6 bytes cada uno)

| Offset | Tipo | Campo |
|---|---|---|
| `0` | `u16` | `first_sprite_index` |
| `2` | `u8`  | `width_in_sprites` |
| `3` | `u8`  | `height_in_sprites` |
| `4` | `u16` | `first_palette_index` |

Un image set es una rejilla de `width_in_sprites × height_in_sprites` sprites, y
puede tener varios *frames* (sub-imágenes) consecutivos. El número de sprites del
set se deduce del `first_sprite_index` del **siguiente** image_def.

**Gotcha de paleta:** el `first_palette_index` se cuenta en **chunks de 4 colores**,
así que el offset en el array de colores es `first_palette_index * 4`. El número de
colores por paleta es `2 ** bpp` (4, 16, 64 o 256), y el `bpp` se toma del primer
sprite del set.

A partir de ahí, por cada frame: colocas cada sprite en su celda de la rejilla,
buscas el color de cada índice en la paleta del set y construyes la imagen RGBA que
vuelcas a PNG.

## Uso del extractor

> El script `tama_extractor.py` espera que **tú** le pases tu propio volcado.

```bash
pip install pillow
python tama_extractor.py mi_dump.bin --out sprites/
```

Genera un PNG por sprite en `sprites/`, más una hoja de contactos para revisar de
un vistazo que todo se decodificó bien.

## Créditos

El reversing original del formato del firmware del Tamagotchi Smart lo hizo
**zenzoa**, no yo. Mi aporte aquí es la re-implementación en Python y este
walkthrough pedagógico. Todo el mérito del descubrimiento del formato es suyo:

- **Smarty Pants** — la herramienta de referencia (Rust, MIT) para leer y editar
  firmware y tarjetas TamaSma: <https://github.com/zenzoa/smartypants>
- Documentación del data format y guías de la comunidad:
  <https://zenzoa.com/pocketfriends/tama-smart.html>

Si este writeup te resultó útil, ve a darle una estrella a su repo. 🌟

## Qué hay y qué no hay en este repo

✅ **Sí:** código de extracción, documentación del formato, este writeup.
❌ **No:** ningún volcado de firmware, ninguna ROM, ningún sprite de Bandai.

Reversear un dispositivo que es tuyo para entenderlo e interoperar es una actividad
legítima y habitual en la comunidad. Redistribuir la ROM o los gráficos de la marca
es otra cosa distinta, y por eso este repo no los incluye.

## Licencia

[MIT](LICENSE). Si reutilizas o portas código de smartypants, recuerda conservar
también su aviso de copyright MIT.

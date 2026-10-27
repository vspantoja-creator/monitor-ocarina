#!/usr/bin/env python3
"""
Monitor de stock - The Legend of Zelda: Ocarina of Time (Nintendo Switch 2) en Chile.

Revisa las paginas de tiendas.json y avisa por Telegram cuando una tienda
pasa a tener stock o preventa abierta. Solo usa la libreria estandar de Python,
asi que no hay nada que instalar.

Modo (variable de entorno MODO):
  auto     -> ejecucion programada: solo avisa cuando algo cambia.
  resumen  -> ejecucion manual: envia el estado de todas las tiendas.

Secretos necesarios (GitHub > Settings > Secrets and variables > Actions):
  TELEGRAM_BOT_TOKEN  token que entrega @BotFather
  TELEGRAM_CHAT_ID    numero del chat (o grupo) que recibe los avisos
"""

import html as htmllib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CARPETA = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_TIENDAS = os.path.join(CARPETA, "tiendas.json")
ARCHIVO_ESTADO = os.path.join(CARPETA, "estado.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Despues de cuantas revisiones fallidas seguidas de una tienda se envia un aviso.
AVISO_TRAS_PROBLEMAS = 6

# Frases que indican que el producto NO se puede comprar (sin tildes, en minusculas).
FRASES_AGOTADO = [
    "agotad",                     # agotado / agotada / preventa agotada
    "sin stock",
    "fuera de stock",
    "sin existencias",
    "no hay producto disponible",
    "producto no disponible",
    "temporalmente no disponible",
    "avisame cuando",
    "notificame cuando",
    "sold out",
    "out of stock",
]

# Frases que indican que el producto SI se puede comprar.
FRASES_DISPONIBLE = [
    "agregar al carr",            # agregar al carro / carrito
    "anadir al carr",             # añadir al carro / carrito (sin tilde)
    "comprar ahora",
    "reservar ahora",
    "add to cart",
    "buy now",
]

ICONOS = {"disponible": "🟢", "agotado": "🔴"}


# --------------------------------------------------------------------------- utilidades

def normalizar(texto):
    """Minusculas y sin tildes, para comparar frases sin problemas."""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def texto_visible(html):
    """Quita scripts, estilos y etiquetas HTML; deja solo el texto que se ve."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|template)\b.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", htmllib.unescape(html)).strip()


def a_precio(valor):
    """Convierte '78.990', '$ 79.990', 78990.0 o '78990' en el entero 78990."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
    else:
        s = str(valor).strip().replace("$", "").replace(" ", "").replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
        elif re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        try:
            numero = float(s)
        except ValueError:
            return None
    numero = int(round(numero))
    return numero if 1_000 <= numero <= 10_000_000 else None


def formato_clp(precio):
    return "precio no detectado" if not precio else "$" + f"{precio:,}".replace(",", ".")


def ahora_chile():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Santiago"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3)))


def descargar(url, acepta="text/html,application/xhtml+xml"):
    """Descarga una pagina. Reintenta una vez si hay un error pasajero."""
    pedido = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": acepta,
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
        "Cache-Control": "no-cache",
    })
    for intento in (1, 2):
        try:
            with urllib.request.urlopen(pedido, timeout=25) as r:
                crudo = r.read(4_000_000)
                charset = r.headers.get_content_charset() or "utf-8"
            return crudo.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or intento == 2:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if intento == 2:
                raise
        time.sleep(3)


# --------------------------------------------------------------------------- deteccion

def estado_desde_texto_schema(valor):
    """Interpreta valores tipo 'https://schema.org/InStock', 'out of stock', 'preorder'."""
    v = normalizar(valor).strip().rstrip("/").rsplit("/", 1)[-1]
    v = re.sub(r"[\s_\-]", "", v)
    if v in ("outofstock", "soldout", "discontinued", "instoreonly", "oos", "unavailable"):
        return "agotado"
    if v in ("instock", "preorder", "presale", "limitedavailability", "onlineonly",
             "backorder", "availablefororder", "available"):
        return "disponible"
    return None


def analizar_shopify(texto_json):
    """Tiendas Shopify (Bestmart, 8-Bits): /products/xxx.js trae 'available' y el precio."""
    datos = json.loads(texto_json)
    disponible = bool(datos.get("available"))
    precio = None
    variantes = datos.get("variants") or []
    candidatas = [v for v in variantes if v.get("available")] or variantes
    bruto = candidatas[0].get("price") if candidatas else datos.get("price")
    if isinstance(bruto, (int, float)):
        # Shopify entrega el precio x100 (78990 CLP -> 7899000).
        precio = a_precio(bruto / 100) or a_precio(bruto)
    return {"estado": "disponible" if disponible else "agotado", "precio": precio,
            "fuente": "datos Shopify"}


def _buscar_productos(nodo):
    if isinstance(nodo, list):
        for x in nodo:
            yield from _buscar_productos(x)
    elif isinstance(nodo, dict):
        tipo = nodo.get("@type")
        tipos = [normalizar(t) for t in (tipo if isinstance(tipo, list) else [tipo]) if t]
        if any(t in ("product", "productgroup", "videogame", "individualproduct") for t in tipos):
            yield nodo
        for clave in ("@graph", "mainEntity"):
            if clave in nodo:
                yield from _buscar_productos(nodo[clave])


def _ofertas(producto):
    salida = []

    def agregar(o):
        if isinstance(o, list):
            for x in o:
                agregar(x)
        elif isinstance(o, dict):
            salida.append(o)
            if "offers" in o:
                agregar(o["offers"])

    agregar(producto.get("offers"))
    variantes = producto.get("hasVariant") or []
    for v in variantes if isinstance(variantes, list) else [variantes]:
        if isinstance(v, dict):
            agregar(v.get("offers"))
    return salida


def senal_jsonld(html, palabra):
    """Datos estructurados schema.org (JSON-LD) del producto principal."""
    productos = []
    bloques = re.findall(
        r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html)
    for bloque in bloques:
        bloque = bloque.strip()
        for intento in (bloque, htmllib.unescape(bloque)):
            try:
                productos.extend(_buscar_productos(json.loads(intento)))
                break
            except ValueError:
                continue
    if not productos:
        return None
    elegidos = [p for p in productos if palabra and palabra in normalizar(p.get("name", ""))]
    if not elegidos:
        if len(productos) != 1:
            return None
        elegidos = productos
    estado, precio = None, None
    for oferta in _ofertas(elegidos[0]):
        e = estado_desde_texto_schema(oferta.get("availability", "")) if oferta.get("availability") else None
        if e == "disponible" or (e == "agotado" and estado is None):
            estado = e
        if precio is None:
            spec = oferta.get("priceSpecification")
            spec_precio = spec.get("price") if isinstance(spec, dict) else None
            precio = a_precio(oferta.get("price")) or a_precio(oferta.get("lowPrice")) or a_precio(spec_precio)
    if estado is None:
        return None
    return {"estado": estado, "precio": precio, "fuente": "datos schema.org"}


def leer_meta(html, nombres):
    for nombre in nombres:
        n = re.escape(nombre)
        for patron in (
            rf'<meta[^>]+(?:property|name|itemprop)=["\']{n}["\'][^>]*content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{n}["\']',
        ):
            m = re.search(patron, html, re.I)
            if m:
                return htmllib.unescape(m.group(1)).strip()
    return None


def senal_meta(html):
    """Etiquetas <meta property="product:availability"> (las usa p. ej. Todojuegos)."""
    valor = leer_meta(html, ["product:availability", "og:availability"])
    estado = estado_desde_texto_schema(valor) if valor else None
    if not estado:
        return None
    return {"estado": estado, "precio": None, "fuente": "etiqueta meta"}


def senal_prestashop(html):
    """Tiendas PrestaShop (Kaio Juegos): datos del producto y estado del boton."""
    m = re.search(r'availability(?:&quot;|")\s*:\s*(?:&quot;|")'
                  r'(available|unavailable|last_remaining_items)', html)
    if m:
        estado = "agotado" if m.group(1) == "unavailable" else "disponible"
        return {"estado": estado, "precio": None, "fuente": "datos PrestaShop"}
    m = re.search(r'(?is)id=["\']product-availability["\'][^>]*>(.*?)</(?:span|div|p)>', html)
    if m:
        texto = normalizar(texto_visible(m.group(1)))
        if any(f in texto for f in FRASES_AGOTADO):
            return {"estado": "agotado", "precio": None, "fuente": "aviso de stock PrestaShop"}
        if "stock" in texto or "disponible" in texto or "preventa" in texto:
            return {"estado": "disponible", "precio": None, "fuente": "aviso de stock PrestaShop"}
    return None


def ventana_producto(html, palabra):
    """Texto que sigue al titulo <h1> del producto (evita leer productos relacionados)."""
    for m in re.finditer(r"(?is)<h1\b[^>]*>(.*?)</h1>", html):
        if palabra and palabra in normalizar(texto_visible(m.group(1))):
            return normalizar(texto_visible(html[m.start():]))[:1500]
    return None


def senal_texto(html, palabra):
    """Ultimo recurso: frases como 'Agotado' o 'Agregar al carro' cerca del titulo."""
    ventana = ventana_producto(html, palabra)
    zona = "texto junto al titulo"
    if ventana is None:
        ventana = normalizar(texto_visible(html))
        zona = "texto de la pagina"
    if any(f in ventana for f in FRASES_AGOTADO):
        estado = "agotado"
    elif any(f in ventana for f in FRASES_DISPONIBLE):
        estado = "disponible"
    else:
        return None
    precio = None
    m = re.search(r"\$\s?(\d{1,3}(?:\.\d{3})+)", ventana)
    if m:
        precio = a_precio(m.group(1))
    return {"estado": estado, "precio": precio, "fuente": zona}


def analizar_html(html, palabra):
    resultado = None
    for senal in (senal_prestashop(html), senal_jsonld(html, palabra),
                  senal_meta(html), senal_texto(html, palabra)):
        if senal:
            resultado = senal
            break
    if not resultado:
        return {"estado": "desconocido", "precio": None, "fuente": "",
                "detalle": "no encontre el estado en la pagina"}
    if not resultado.get("precio"):
        resultado["precio"] = (
            a_precio(leer_meta(html, ["product:price:amount", "og:price:amount"]))
            or (senal_texto(html, palabra) or {}).get("precio")
        )
    return resultado


def revisar_tienda(tienda, palabra):
    url = tienda["url"]
    ruta = urllib.parse.urlsplit(url).path
    try:
        if "/products/" in ruta:
            base = url.split("#")[0].split("?")[0].rstrip("/")
            try:
                return analizar_shopify(descargar(base + ".js", "application/json"))
            except Exception as e:  # si falla, se intenta con la pagina normal
                print(f"   ({tienda['nombre']}: sin datos Shopify, {type(e).__name__}; leo la pagina)")
        return analizar_html(descargar(url), palabra)
    except urllib.error.HTTPError as e:
        return {"estado": "error", "precio": None, "fuente": "", "detalle": f"HTTP {e.code}"}
    except Exception as e:
        return {"estado": "error", "precio": None, "fuente": "", "detalle": type(e).__name__}


# --------------------------------------------------------------------------- mensajes

def esc(texto):
    return htmllib.escape(str(texto), quote=True)


def mensaje_stock(producto, tienda, precio):
    return (
        f"🟢 <b>¡HAY STOCK!</b> {esc(producto)}\n"
        f"🏪 <b>{esc(tienda['nombre'])}</b> — {esc(formato_clp(precio))}\n"
        f"👉 {esc(tienda['url'])}\n\n"
        f"Confírmalo en la tienda antes de pagar."
    )


def mensaje_agotado(producto, tienda):
    return f"🔴 Se agotó en <b>{esc(tienda['nombre'])}</b> ({esc(producto)})."


def mensaje_problema(tienda, detalle):
    return (
        f"⚠️ No he podido revisar <b>{esc(tienda['nombre'])}</b> en las últimas "
        f"{AVISO_TRAS_PROBLEMAS} revisiones ({esc(detalle)}).\n"
        f"Puede que la tienda cambió la página o bloqueó al monitor. Revísala a mano:\n"
        f"{esc(tienda['url'])}"
    )


def mensaje_resumen(producto, filas):
    lineas = [f"📋 <b>{esc(producto)}</b>", f"Revisión {ahora_chile():%d/%m %H:%M}", ""]
    for tienda, r in filas:
        enlace = f'<a href="{esc(tienda["url"])}">{esc(tienda["nombre"])}</a>'
        if r["estado"] in ICONOS:
            texto = "DISPONIBLE" if r["estado"] == "disponible" else "agotado"
            lineas.append(f"{ICONOS[r['estado']]} {enlace} — {texto} — {esc(formato_clp(r.get('precio')))}")
        else:
            lineas.append(f"⚪ {enlace} — sin dato ({esc(r.get('detalle', ''))})")
    lineas += ["", "Los avisos automáticos llegan solo cuando algo cambia."]
    return "\n".join(lineas)


def enviar_telegram(token, chat_id, texto):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    cuerpo = json.dumps({"chat_id": chat_id, "text": texto, "parse_mode": "HTML",
                         "disable_web_page_preview": True}).encode("utf-8")
    pedido = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=20) as r:
            respuesta = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            motivo = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            motivo = ""
        raise RuntimeError(f"Telegram respondió {e.code} {motivo}".strip()) from None
    if not respuesta.get("ok"):
        raise RuntimeError(f"Telegram rechazó el mensaje: {respuesta.get('description')}")


# --------------------------------------------------------------------------- logica principal

def comparar(previo, filas, producto):
    """Decide que avisos enviar comparando con la revision anterior."""
    nuevo, avisos = {}, []
    for tienda, r in filas:
        anterior = previo.get(tienda["nombre"], {})
        estado_ant = anterior.get("estado")
        if r["estado"] in ("disponible", "agotado"):
            registro = {"estado": r["estado"], "precio": r.get("precio") or anterior.get("precio"),
                        "problemas": 0}
            if r["estado"] == "disponible" and estado_ant != "disponible":
                avisos.append(mensaje_stock(producto, tienda, registro["precio"]))
            elif r["estado"] == "agotado" and estado_ant == "disponible":
                avisos.append(mensaje_agotado(producto, tienda))
        else:
            problemas = min(anterior.get("problemas", 0) + 1, AVISO_TRAS_PROBLEMAS)
            registro = {"estado": estado_ant, "precio": anterior.get("precio"), "problemas": problemas}
            if problemas == AVISO_TRAS_PROBLEMAS and anterior.get("problemas", 0) < AVISO_TRAS_PROBLEMAS:
                avisos.append(mensaje_problema(tienda, r.get("detalle", "")))
        nuevo[tienda["nombre"]] = registro
    return nuevo, avisos


def escribir_resumen_github(producto, filas, nota=""):
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    lineas = [f"### {producto}", ""]
    if nota:
        lineas += [nota, ""]
    if filas:
        lineas += ["| Tienda | Estado | Precio | Cómo se detectó |", "|---|---|---|---|"]
        for tienda, r in filas:
            estado = r["estado"] if r["estado"] != "error" else f"error ({r.get('detalle', '')})"
            lineas.append(f"| [{tienda['nombre']}]({tienda['url']}) | {estado} | "
                          f"{formato_clp(r.get('precio'))} | {r.get('fuente') or r.get('detalle', '')} |")
    with open(destino, "a", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")


def main():
    modo = (os.environ.get("MODO") or "auto").strip().lower()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

    with open(ARCHIVO_TIENDAS, encoding="utf-8") as f:
        config = json.load(f)
    producto = config.get("producto", "Producto")
    palabra = normalizar(config.get("palabra_clave", ""))
    tiendas = [t for t in config["tiendas"] if t.get("activa", True)]

    if not token or not chat_id:
        nota = ("Falta configurar los secretos TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID "
                "(Settings > Secrets and variables > Actions).")
        escribir_resumen_github(producto, [], "⚠️ " + nota)
        if modo == "resumen":
            print(f"::error::{nota}")
            return 1
        print(f"::warning::{nota} No se revisó nada.")
        return 0

    previo = {}
    if os.path.exists(ARCHIVO_ESTADO):
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            previo = json.load(f)

    filas = []
    for tienda in tiendas:
        r = revisar_tienda(tienda, palabra)
        filas.append((tienda, r))
        print(f"- {tienda['nombre']}: {r['estado']} | {formato_clp(r.get('precio'))} | "
              f"{r.get('fuente') or r.get('detalle', '')}")

    nuevo, avisos = comparar(previo, filas, producto)
    escribir_resumen_github(producto, filas)

    mensajes = [mensaje_resumen(producto, filas)] if modo == "resumen" else avisos
    try:
        for m in mensajes:
            enviar_telegram(token, chat_id, m)
    except Exception as e:
        # No se guarda el estado: en la proxima revision se vuelve a intentar el aviso.
        print(f"::error::No se pudo enviar el aviso por Telegram: {e}")
        return 1
    print(f"Mensajes enviados a Telegram: {len(mensajes)}")

    if nuevo != previo:
        with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
            json.dump(nuevo, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

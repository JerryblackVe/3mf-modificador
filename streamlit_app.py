import streamlit as st
import zipfile, io, re, hashlib
from datetime import datetime

st.set_page_config(page_title="Bambu 3MF — Change Plates Editor", page_icon="🛠️", layout="centered")

st.title("Bambu 3MF — Change Plates Editor")
st.write("Sube un **.3mf** de BambuLab, ajusta la **cantidad de ciclos** y los **mm de Z** en todas las secciones `change plates`, y descarga el 3MF modificado.")

# Delimitadores de sección
SECTION_RE = re.compile(
    r"(;=+\s*Starting\s+to\s+change\s+plates[^\n]*\n)(.*?)(;=+\s*Finish\s+to\s+change\s+plates[^\n]*\n)",
    re.IGNORECASE | re.DOTALL,
)
# Ciclos Z: G380 S3 (down) / G380 S2 (up)
ZDOWN_RE = re.compile(r"^\s*G380\s+S3\s+Z-?\s*(?P<down>[0-9]+(?:\.[0-9]+)?)\s+F[0-9.]+\s*(?:;.*)?$", re.IGNORECASE)
ZUP_RE   = re.compile(r"^\s*G380\s+S2\s+Z\s*(?P<up>[0-9]+(?:\.[0-9]+)?)\s+F[0-9.]+\s*(?:;.*)?$", re.IGNORECASE)

def find_cycles(lines):
    """Encuentra bloque contiguo de pares ZDOWN/ZUP. Devuelve (start, end, [(down, up), ...])."""
    i = 0
    while i < len(lines):
        if ZDOWN_RE.match(lines[i] or ""):
            break
        i += 1
    start = i
    cycles = []
    while i + 1 < len(lines):
        m_down = ZDOWN_RE.match(lines[i] or "")
        m_up   = ZUP_RE.match(lines[i+1] or "")
        if not (m_down and m_up):
            break
        down = float(m_down.group("down"))
        up   = float(m_up.group("up"))
        cycles.append((down, up))
        i += 2
    end = i
    if cycles:
        return start, end, cycles
    return None, None, []

def rebuild_cycles(desired_cycles:int, down_mm:float, up_mm:float, example_down_line:str, example_up_line:str):
    """Reconstruye N ciclos preservando feedrates/comentarios del ejemplo."""
    def extract_F(line, default=" F1200"):
        m = re.search(r"\sF([0-9.]+)", line, re.IGNORECASE)
        return f" F{m.group(1)}" if m else default

    f_down = extract_F(example_down_line)
    f_up   = extract_F(example_up_line)

    comment_down = ""
    mcd = re.search(r"(;.*)$", example_down_line)
    if mcd: comment_down = " " + mcd.group(1).lstrip()

    comment_up = ""
    mcu = re.search(r"(;.*)$", example_up_line)
    if mcu: comment_up = " " + mcu.group(1).lstrip()

    lines = []
    for _ in range(desired_cycles):
        lines.append(f"G380 S3 Z-{down_mm}{f_down}{(' ' + comment_down) if comment_down and not comment_down.startswith(';') else comment_down}".rstrip() + "\n")
        lines.append(f"G380 S2 Z{up_mm}{f_up}{(' ' + comment_up) if comment_up and not comment_up.startswith(';') else comment_up}".rstrip() + "\n")
    return lines

def md5_bytes(b:bytes):
    h = hashlib.md5(); h.update(b); return h.hexdigest()

def process_gcode(gcode_bytes, desired_cycles:int, down_mm:float, up_mm:float, report:list):
    """Procesa un plate_*.gcode: reemplaza ciclos dentro de cada sección change plates."""
    text = gcode_bytes.decode("utf-8", errors="ignore")
    changed = False

    def _replace_section(match):
        nonlocal changed
        head, body, tail = match.group(1), match.group(2), match.group(3)
        lines = body.splitlines(keepends=True)

        s, e, cycles = find_cycles([ln.rstrip('\n') for ln in lines])
        if not cycles:
            report.append("Sección encontrada pero no se identificaron ciclos G380 S3/S2 contiguos; se deja sin cambios.")
            return head + body + tail

        example_down = lines[s].rstrip("\n")
        example_up   = lines[s+1].rstrip("\n")
        new_cycle_lines = rebuild_cycles(desired_cycles, down_mm, up_mm, example_down, example_up)

        new_body = "".join(lines[:s]) + "".join(new_cycle_lines) + "".join(lines[e:])
        changed = True
        report.append(f"Actualizados ciclos en una sección: {len(cycles)} -> {desired_cycles} | down={down_mm} | up={up_mm}.")
        return head + new_body + tail

    new_text, n = SECTION_RE.subn(_replace_section, text)
    if n == 0:
        report.append("No se encontraron secciones 'change plates' en este GCODE.")
    elif changed:
        return new_text.encode("utf-8"), True
    return gcode_bytes, False

def process_3mf(src_bytes: bytes, desired_cycles:int, down_mm:float, up_mm:float):
    """Carga 3MF en memoria, modifica todos los Metadata/plate_*.gcode y recalcula .md5."""
    in_mem = io.BytesIO(src_bytes)
    zin = zipfile.ZipFile(in_mem, "r")

    out_mem = io.BytesIO()
    zout = zipfile.ZipFile(out_mem, "w", compression=zipfile.ZIP_DEFLATED)

    report = []
    modified_files = 0

    for info in zin.infolist():
        data = zin.read(info.filename)
        lower = info.filename.lower()
        if lower.startswith("metadata/") and lower.endswith(".gcode"):
            new_data, changed = process_gcode(data, desired_cycles, down_mm, up_mm, report)
            zout.writestr(info, new_data)
            if changed:
                modified_files += 1
        else:
            zout.writestr(info, data)

    zout.close()
    zin.close()

    # Reabrir, recomputar MD5 si existen archivos .md5
    in2 = io.BytesIO(out_mem.getvalue())
    ztmp = zipfile.ZipFile(in2, "r")
    out_final = io.BytesIO()
    zfinal = zipfile.ZipFile(out_final, "w", compression=zipfile.ZIP_DEFLATED)

    file_cache = {info.filename: ztmp.read(info.filename) for info in ztmp.infolist()}
    ztmp.close()

    for name in list(file_cache.keys()):
        if name.lower().startswith("metadata/plate_") and name.lower().endswith(".gcode.md5"):
            gcode_name = name[:-4]
            if gcode_name in file_cache:
                digest = md5_bytes(file_cache[gcode_name]) + "\n"
                file_cache[name] = digest.encode("ascii")

    for name, data in file_cache.items():
        zfinal.writestr(name, data)

    ts = datetime.utcnow().isoformat() + "Z"
    report_txt = f"# Reporte de modificación ({ts})\n"
    report_txt += f"Archivos GCODE modificados: {modified_files}\n"
    for r in report:
        report_txt += f"- {r}\n"
    zfinal.writestr("Metadata/change_plates_report.txt", report_txt.encode("utf-8"))
    zfinal.close()
    out_final.seek(0)
    return out_final.getvalue(), modified_files, report

# --- UI ---
st.subheader("Parámetros")
col1, col2, col3 = st.columns(3)
with col1:
    cycles = st.number_input("Ciclos (repeticiones)", min_value=0, value=4, step=1)
with col2:
    down_mm = st.number_input("Descenso Z (mm)", min_value=0.0, value=20.0, step=0.5, format="%.1f")
with col3:
    up_mm = st.number_input("Ascenso Z (mm)", min_value=0.0, value=75.0, step=0.5, format="%.1f")

uploaded = st.file_uploader("Archivo .3mf", type=["3mf"])

if uploaded is not None:
    st.info(f"Archivo: **{uploaded.name}** ({uploaded.size/1024:.1f} KB)")
    if st.button("Procesar 3MF"):
        try:
            result_bytes, modified, report = process_3mf(uploaded.read(), int(cycles), float(down_mm), float(up_mm))
            st.success(f"Listo. GCODEs modificados: {modified}.")
            st.download_button(
                label="Descargar 3MF modificado",
                data=result_bytes,
                file_name=f"modified_{uploaded.name}",
                mime="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
            )
            with st.expander("Ver reporte"):
                st.code("\n".join(report) if report else "Sin cambios.", language="text")
        except Exception as e:
            st.error(f"Error procesando el archivo: {e}")
else:
    st.caption("Sube un archivo para habilitar el procesamiento.")

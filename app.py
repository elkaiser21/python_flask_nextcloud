import logging
import os

import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import unquote
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import mysql.connector
import requests

import folium
import h3
from branca.colormap import linear

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    abort,
    Response,
)

import geo_utils
import classes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

GPX_DIR = "/home/w7667388/trackfiles"

NEXTCLOUD_USER = os.environ.get("NEXTCLOUD_USER")
NEXTCLOUD_PASSWORD = os.environ.get("NEXTCLOUD_PASSWORD")
NEXTCLOUD_DAV_URL = os.environ.get("NEXTCLOUD_DAV_URL")
NEXTCLOUD_PHOTO_DIR = os.environ.get("NEXTCLOUD_PHOTO_SYNC_DIR")
NEXTCLOUD_PHOTO_ARCHIVE_DIR = os.environ.get("NEXTCLOUD_PHOTO_ARCHIVE_DIR")
NEXTCLOUD_TRACK_DIR = os.environ.get("NEXTCLOUD_TRACK_SYNC_DIR")

# centralized requests timeout (seconds)
REQUEST_TIMEOUT = 60


@app.context_processor
def inject_path():
    return {"current_path": request.path}


app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

db_config = {
    "host": "localhost",
    "user": os.environ.get("DB_USER"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME"),
}


def get_user(username):
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    cursor.close()
    conn.close()
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def get_tracks_by_latlon(location, radius):
    """
    A location string and a radius in km is selecting all the tracks within that area.
    For the selection of tracks the middle lat-lon is used
    """
    center_lat, center_lon, message = geo_utils.geocode_location(location)
    if message:
        return [], message
    min_lat, max_lat, min_lon, max_lon = geo_utils.bounding_box(
        center_lat, center_lon, radius
    )

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    query = """
    SELECT
            t.name,
            t.date_taken,
            t.extra_info,
            t.type,
            ROUND(t.length/1000, 2) as len,
            ROUND(t.middle_lat, 6) as lat,
            ROUND(t.middle_lon, 6) as lon,
        (
            6371 * ACOS(
                COS(RADIANS(%s)) *
                COS(RADIANS(t.middle_lat)) *
                COS(RADIANS(t.middle_lon) - RADIANS(%s)) +
                SIN(RADIANS(%s)) *
                SIN(RADIANS(t.middle_lat))
            )
        ) AS distance
    FROM track t
    WHERE
        t.middle_lat BETWEEN %s AND %s
        AND t.middle_lon BETWEEN %s AND %s
    HAVING distance <= %s
    ORDER BY distance;
    """

    params = (
        center_lat,
        center_lon,
        center_lat,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
        radius,
    )

    cursor.execute(query, params)
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results, message


def get_tracks_by_keyword(keywords, extra_info=None, min_len_m=None, max_len_m=None):
    """
    Select all tracks with the given (part of) keyword.
    For a single keyword, use a case-insensitive LIKE match; for multiple keywords, use the existing IN-based match.
    Optional DB-level filtering by extra_info (substring, case-insensitive) and length in meters.
    """
    normalized_keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not normalized_keywords:
        return []

    params = []
    where_clauses = []

    if len(normalized_keywords) == 1:
        kw = normalized_keywords[0].lower()
        where_clauses.append("LOWER(keyword) LIKE %s")
        params.append(f"%{kw}%")
    else:
        placeholders = ",".join(["%s"] * len(normalized_keywords))
        where_clauses.append(f"LOWER(keyword) IN ({placeholders})")
        params.extend([kw.lower() for kw in normalized_keywords])
        params.append(len(normalized_keywords))

    if extra_info:
        where_clauses.append("LOWER(extra_info) LIKE %s")
        params.append(f"%{extra_info.lower()}%")
    if min_len_m is not None:
        where_clauses.append("length >= %s")
        params.append(min_len_m)
    if max_len_m is not None:
        where_clauses.append("length <= %s")
        params.append(max_len_m)

    where_sql = " AND ".join(where_clauses)

    if len(normalized_keywords) > 1:
        sql = f"""
            SELECT DISTINCT
                name,
                date_taken,
                extra_info,
                type,
                ROUND(length/1000, 2) as len,
                ROUND(middle_lat, 6) as lat,
                ROUND(middle_lon, 6) as lon
            FROM v_tracks_by_keyword
            WHERE {where_sql}
            GROUP BY name
            HAVING COUNT(DISTINCT keyword) = %s
            ORDER BY date_taken DESC
        """
    else:
        sql = f"""
            SELECT DISTINCT
                name,
                date_taken,
                extra_info,
                type,
                ROUND(length/1000, 2) as len,
                ROUND(middle_lat, 6) as lat,
                ROUND(middle_lon, 6) as lon
            FROM v_tracks_by_keyword
            WHERE {where_sql}
            GROUP BY name
            ORDER BY date_taken DESC
        """

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, tuple(params))
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results


def get_tracks_by_date(selected_date):
    """
    Select all tracks with the given date.
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    sql = """
        SELECT
            name,
            date_taken,
            extra_info,
            type,
            ROUND(length/1000, 2) as len,
            ROUND(middle_lat, 6) as lat,
            ROUND(middle_lon, 6) as lon
        FROM track
        WHERE DATE(date_taken) = %s
      """

    cursor.execute(sql, (selected_date,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    return results


def get_tracks_by_date_range(start_date, end_date, extra_info=None, min_len_m=None, max_len_m=None):
    """
    Select all tracks between start_date and end_date (inclusive).
    start_date and end_date should be strings in YYYY-MM-DD format or date objects.
    Optional DB-level filtering by extra_info (substring, case-insensitive) and length in meters.
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    where_clauses = ["DATE(date_taken) BETWEEN %s AND %s"]
    params = [f"{start_date}", f"{end_date}"]

    if extra_info:
        where_clauses.append("LOWER(extra_info) LIKE %s")
        params.append(f"%{extra_info.lower()}%")
    if min_len_m is not None:
        where_clauses.append("length >= %s")
        params.append(min_len_m)
    if max_len_m is not None:
        where_clauses.append("length <= %s")
        params.append(max_len_m)

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT
            name,
            date_taken,
            extra_info,
            type,
            ROUND(length/1000, 2) as len,
            ROUND(middle_lat, 6) as lat,
            ROUND(middle_lon, 6) as lon
        FROM track
        WHERE {where_sql}
        ORDER BY date_taken DESC
      """

    cursor.execute(sql, tuple(params))
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    return results


def get_gpx_points(year, month, day, filename):
    """
    Downloads the GPX file from Nextcloud for the given date and filename and
    returns a list of (lat, lon) tuples extracted from trkpt elements.
    """
    try:
        url = get_nextcloud_url(NEXTCLOUD_TRACK_DIR, year, month, day, filename)
        r = requests.get(url, auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD), timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []

        data = r.content
        root = ET.fromstring(data)
        ns = {}
        if root.tag.startswith("{"):
            # namespace-aware GPX
            uri = root.tag.split("}")[0].strip("{")
            ns = {"g": uri}

        points = []
        # find all trkpt elements
        if ns:
            for trkpt in root.findall('.//g:trkpt', ns):
                lat = float(trkpt.attrib.get('lat'))
                lon = float(trkpt.attrib.get('lon'))
                points.append((lat, lon))
        else:
            for trkpt in root.findall('.//trkpt'):
                lat = float(trkpt.attrib.get('lat'))
                lon = float(trkpt.attrib.get('lon'))
                points.append((lat, lon))

        return points
    except Exception:
        return []


def get_keywords_for_tracks(track_names):
    """
    Fetch keywords for a list of track names. Returns a dict: {track_name: [kw,...]}
    """
    if not track_names:
        return {}
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(track_names))
    sql = f"SELECT track_name, keyword FROM track_keywords WHERE track_name IN ({placeholders})"
    cursor.execute(sql, tuple(track_names))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    mapping = {}
    for row in rows:
        name = row.get('track_name')
        kw = row.get('keyword')
        mapping.setdefault(name, []).append(kw)
    return mapping


def get_photos_by_keywords(keywords):
    """
    Select all photos with the given (part of) keyword.
    """
    placeholders = ",".join(["%s"] * len(keywords))
    params = keywords + [len(keywords)]
    # logger.info("placeholders: %r", placeholders)
    # logger.info("params: %r", params)

    sql = f"""
        SELECT DISTINCT
            name,
            date,
            extra_info,
            title,
            ROUND(latitude, 6) as lat,
            ROUND(longitude, 6) as lon
        FROM v_photo_by_keyword
        WHERE LOWER(keyword) IN ({placeholders})
        GROUP BY name
        HAVING COUNT(DISTINCT keyword) = %s
        ORDER BY date DESC
    """

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params)
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results


def get_photo_info(filename, date_taken):
    """
    Get photo information
    """
    sql = """
        SELECT
          f.id,
          f.date_taken,
          f.title,
          f.extra_info,
          f.filename,
          GROUP_CONCAT(k.keyword ORDER BY k.keyword SEPARATOR ', ') AS keywords
        FROM foto f
        LEFT JOIN foto_keywords k
          ON k.foto_id = f.id
        WHERE f.filename = %s AND f.date_taken = %s 
        GROUP BY
          f.id,
          f.date_taken,
          f.title,
          f.extra_info,
          f.filename;
        """

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (f"{filename}", f"{date_taken}"))
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results


def get_keywords_by_searchstring(searchstring, mode):
    """
    Select all keywords with the given (part of) keyword.
    Depending on the choosen mode it with be the keywords of tracks or photos
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    sql = ""

    if mode == "photo":
        sql = """
         SELECT
            keyword,
            COUNT(*) as amount
          FROM foto_keywords
          WHERE LOWER(keyword) LIKE %s
          GROUP BY keyword
          ORDER BY amount DESC
      """
    elif mode == "track":
        sql = """
         SELECT
            keyword,
            COUNT(*) as amount
          FROM track_keywords
          WHERE LOWER(keyword) LIKE %s
          GROUP BY keyword
          ORDER BY amount DESC
      """

    cursor.execute(sql, (f"%{searchstring}%",))
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results


def get_dates_by_searchstring(searchstring, mode):
    """
    Select all dates with the given (part of) datestring.
    Depending on the choosen mode it with be the dates of tracks or photos
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    sql = ""

    if mode == "photo":
        sql = """
         SELECT
            date_taken,
            COUNT(*) as amount
          FROM foto
          WHERE date_taken LIKE %s
          GROUP BY date_taken
          ORDER BY date_taken DESC
      """
    elif mode == "track":
        sql = """
         SELECT
            date_taken,
            COUNT(*) as amount
          FROM track
          WHERE date_taken LIKE %s
          GROUP BY date_taken
          ORDER BY date_taken DESC
      """

    cursor.execute(sql, (f"%{searchstring}%",))
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return results


def get_photos_by_date(selected_date):
    """
    Select all photos with the given date.
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    sql = """
        SELECT
            filename as name,
            date_taken,
            extra_info,
            title,
            ROUND(lat, 6) as lat,
            ROUND(lon, 6) as lon
        FROM foto
        WHERE DATE(date_taken) = %s
    """

    cursor.execute(sql, (selected_date,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    return results


def check_if_track_exists(selected_date):
    """
    See if there is a track for the given date
    """
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    sql = """
        SELECT count(*) as count
        FROM track
        WHERE DATE(date_taken) = %s
    """
    cursor.execute(sql, (selected_date,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    return results


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = get_user(username)

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))

        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("home.html")


@app.route("/track_by_keyword", methods=["GET", "POST"])
@login_required
def track_by_keyword():
    tracks = []
    keyword_str = request.args.get("keyword", "").strip()

    if keyword_str:
        """keywords can be split by comma"""
        keywords = [keyword.strip() for keyword in keyword_str.split(",")]
        tracks = get_tracks_by_keyword(keywords)

    return render_template("track_by_keyword.html", tracks=tracks, keyword=keyword_str)


@app.route("/show_tracks", methods=["GET", "POST"])
@login_required
def show_tracks():
    """
    Shows tracks filtered by start_date, end_date, keyword. Allows selecting tracks to display on a map.
    """
    tracks = []
    start_date = request.values.get("start_date", "").strip()
    end_date = request.values.get("end_date", "").strip()
    keyword_str = request.values.get("keyword", "").strip()
    # allow filtering by track types (multiple checkboxes named track_type)
    track_types = request.values.getlist("track_type") if request.values else []
    # normalize to a set of lowercase values for comparison
    track_types_set = {t.lower() for t in track_types}

    # additional optional filters: extra_info substring and length range
    extra_info = request.values.get("extra_info", "").strip()
    min_length = request.values.get("min_length", "").strip()
    max_length = request.values.get("max_length", "").strip()
    # parse numeric bounds if provided
    try:
        min_length_val = float(min_length) if min_length != "" else None
    except Exception:
        min_length_val = None
    try:
        max_length_val = float(max_length) if max_length != "" else None
    except Exception:
        max_length_val = None

    if request.method == "GET":
        # Populate the track list based on filters
        # convert length km filters to meters for DB filtering
        min_len_m = None
        max_len_m = None
        if min_length_val is not None:
            min_len_m = int(min_length_val * 1000)
        if max_length_val is not None:
            max_len_m = int(max_length_val * 1000)

        if keyword_str:
            keywords = [k.strip() for k in keyword_str.split(",") if k.strip()]
            tracks = get_tracks_by_keyword(keywords, extra_info=extra_info, min_len_m=min_len_m, max_len_m=max_len_m)
            # Optionally filter by date range if provided (DB query does date range filtering when used below)
            if start_date or end_date:
                sd = start_date or "0001-01-01"
                ed = end_date or "9999-12-31"
                # filter by date fields
                filtered = []
                for t in tracks:
                    td = t["date_taken"]
                    try:
                        td_date = (
                            td.date()
                            if hasattr(td, "date")
                            else datetime.strptime(str(td), "%Y-%m-%d").date()
                        )
                    except Exception:
                        td_date = None
                    if td_date:
                        if sd <= str(td_date) <= ed:
                            filtered.append(t)
                tracks = filtered
        elif start_date or end_date:
            sd = start_date or end_date
            ed = end_date or start_date
            tracks = get_tracks_by_date_range(sd, ed, extra_info=extra_info, min_len_m=min_len_m, max_len_m=max_len_m)

        # If track type filters provided, apply them client-side here
        if track_types_set:
            filtered_tracks = []
            for t in tracks:
                ttype = (t.get("type") or "").lower()
                if ttype in track_types_set:
                    filtered_tracks.append(t)
            tracks = filtered_tracks

        # fetch keywords for displayed tracks and attach
        track_names = [t.get('name') for t in tracks if t.get('name')]
        keywords_map = get_keywords_for_tracks(track_names)
        for t in tracks:
            t['keywords'] = keywords_map.get(t.get('name'), [])

        selected = request.values.getlist("selected") if request.values else []
        return render_template(
            "show_tracks.html",
            tracks=tracks,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword_str,
            map_available=False,
            selected=selected,
            track_types=track_types,
            extra_info=extra_info,
            min_length=min_length,
            max_length=max_length,
        )

    # POST - generate map for selected tracks
    selected = request.form.getlist("selected")
    if not selected:
        return render_template(
            "show_tracks.html",
            tracks=[],
            start_date=start_date,
            end_date=end_date,
            keyword=keyword_str,
            map_available=False,
            message="No tracks selected",
            track_types=track_types,
            extra_info=extra_info,
            min_length=min_length,
            max_length=max_length,
        )

    # strong/vivid colors for better contrast on the map
    colors = [
        "#e6194b",
        "#4363d8",
        "#f58231",
        "#911eb4",
        "#46f0f0",
        "#f032e6",
        "#ffe119",
        "#008080",
        "#000000",
    ]
    m = folium.Map(tiles="OpenStreetMap")
    all_points = []

    for idx, val in enumerate(selected):
        # expected format: date::filename
        try:
            date_part, filename = val.split("::", 1)
            # parse date to year/month/day
            dt = datetime.fromisoformat(date_part)
            year = dt.year
            month = dt.month
            day = dt.day
        except Exception:
            # fallback: try splitting by space if any
            parts = val.split("::")
            if len(parts) >= 2:
                date_part = parts[0]
                filename = parts[1]
                dt = datetime.fromisoformat(date_part)
                year = dt.year
                month = dt.month
                day = dt.day
            else:
                continue

        points = get_gpx_points(year, month, day, filename)
        if not points:
            continue

        all_points.extend(points)
        color = colors[idx % len(colors)]
        folium.PolyLine(
            locations=[(lat, lon) for lat, lon in points],
            color=color,
            weight=4,
            opacity=0.8,
            tooltip=filename,
        ).add_to(m)

    if all_points:
        # robust bounding and zoom handling to avoid world-map hang
        import math

        lats = [p[0] for p in all_points]
        lons = [p[1] for p in all_points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        # handle International Date Line crossing
        lon_span = max_lon - min_lon
        adjusted = False
        if lon_span > 180:
            # shift longitudes > 0 by -360 for correct span calc
            lons_adj = [lon - 360 if lon > 0 else lon for lon in lons]
            min_lon, max_lon = min(lons_adj), max(lons_adj)
            lon_span = max_lon - min_lon
            adjusted = True

        lat_span = max_lat - min_lat

        # compute center
        center_lat = sum(lats) / len(lats)
        if adjusted:
            center_lon = (min_lon + max_lon) / 2
            if center_lon < -180:
                center_lon += 360
        else:
            center_lon = sum(lons) / len(lons)

        # determine a reasonable zoom level from span (heuristic)
        span_deg = max(lat_span, lon_span)
        if span_deg == 0:
            zoom = 15
        else:
            try:
                zoom = int(max(2, min(15, round(math.log2(360.0 / span_deg)))))
            except Exception:
                zoom = 12

        # apply center and zoom
        m.location = [center_lat, center_lon]
        m.zoom_start = zoom

        # build bounds (normalize longitudes for leaflet if needed)
        if adjusted:
            bounds = [[min_lat, min_lon], [max_lat, max_lon]]
            bounds = [[b[0], (b[1] + 360) if b[1] < -180 else b[1]] for b in bounds]
        else:
            bounds = [[min_lat, min_lon], [max_lat, max_lon]]

        # try fit_bounds with padding; if it fails, keep explicit center/zoom
        try:
            m.fit_bounds(bounds, padding=(50, 50))
        except Exception:
            pass

    # ensure maps dir exists and save
    maps_dir = os.path.join("static", "maps")
    os.makedirs(maps_dir, exist_ok=True)
    map_path = os.path.join(maps_dir, "show_tracks.html")
    m.save(map_path)

    return render_template(
        "show_tracks.html",
        tracks=[],
        start_date=start_date,
        end_date=end_date,
        keyword=keyword_str,
        map_available=True,
        map_url="/" + map_path,
        selected=selected,
        track_types=track_types,
        extra_info=extra_info,
        min_length=min_length,
        max_length=max_length,
    )


@app.route("/photo_by_keyword", methods=["GET", "POST"])
@login_required
def photo_by_keyword():
    photos = []
    keyword_str = request.args.get("keyword", "").strip()

    if keyword_str:
        """keywords can be split by comma"""
        keywords = [keyword.strip() for keyword in keyword_str.split(",")]
        photos = get_photos_by_keywords(keywords)

    return render_template("photo_by_keyword.html", photos=photos, keyword=keyword_str)


@app.route("/photo_by_date", methods=["GET"])
@login_required
def photo_by_date():
    date_str = request.args.get("date")  # e.g. "2025-01-15"
    showphotos = request.args.get("showphotos")
    photos = []
    tracks_exists = False

    if date_str:
        if showphotos:
            year, month, day = map(int, date_str.split("-"))
            return redirect(
                url_for(
                    "photos_by_date",
                    year=year,
                    month=month,
                    day=day,
                )
            )
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        photos = get_photos_by_date(selected_date)
        row = check_if_track_exists(selected_date)[0]
        tracks_exists = bool(row["count"]) if row else False

    return render_template(
        "photo_by_date.html",
        photos=photos,
        selected_date=date_str,
        showphotos=showphotos,
        tracks_exists=tracks_exists,
    )


@app.route("/track_by_location", methods=["GET", "POST"])
@login_required
def track_by_location():
    tracks = []
    location = ""
    message = ""
    kmradius = 5
    selected = []

    if request.method == "POST":
        # Accept both search and show-on-map actions via POST
        location = request.form.get("location", "").strip()
        kmradius = request.form.get("kmradius", 5).strip()
        # If user clicked Show on map, selected checkboxes will be present
        selected = request.form.getlist("selected")
        if selected:
            # generate map for selected tracks
            colors = [
                "#e6194b",
                "#4363d8",
                "#f58231",
                "#911eb4",
                "#46f0f0",
                "#f032e6",
                "#ffe119",
                "#008080",
                "#000000",
            ]
            m = folium.Map(tiles="OpenStreetMap")
            all_points = []
            for idx, val in enumerate(selected):
                try:
                    date_part, filename = val.split("::", 1)
                    dt = datetime.fromisoformat(date_part)
                    year, month, day = dt.year, dt.month, dt.day
                except Exception:
                    parts = val.split("::")
                    if len(parts) >= 2:
                        date_part = parts[0]
                        filename = parts[1]
                        try:
                            dt = datetime.fromisoformat(date_part)
                            year, month, day = dt.year, dt.month, dt.day
                        except Exception:
                            continue
                    else:
                        continue
                points = get_gpx_points(year, month, day, filename)
                if not points:
                    continue
                all_points.extend(points)
                color = colors[idx % len(colors)]
                folium.PolyLine(
                    locations=[(lat, lon) for lat, lon in points],
                    color=color,
                    weight=4,
                    opacity=0.8,
                    tooltip=filename,
                ).add_to(m)
            if all_points:
                # reuse robust fit logic used elsewhere
                import math
                lats = [p[0] for p in all_points]
                lons = [p[1] for p in all_points]
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                lon_span = max_lon - min_lon
                adjusted = False
                if lon_span > 180:
                    lons_adj = [lon - 360 if lon > 0 else lon for lon in lons]
                    min_lon, max_lon = min(lons_adj), max(lons_adj)
                    lon_span = max_lon - min_lon
                    adjusted = True
                lat_span = max_lat - min_lat
                center_lat = sum(lats) / len(lats)
                if adjusted:
                    center_lon = (min_lon + max_lon) / 2
                    if center_lon < -180:
                        center_lon += 360
                else:
                    center_lon = sum(lons) / len(lons)
                span_deg = max(lat_span, lon_span)
                if span_deg == 0:
                    zoom = 15
                else:
                    try:
                        zoom = int(max(2, min(15, round(math.log2(360.0 / span_deg)))))
                    except Exception:
                        zoom = 12
                m.location = [center_lat, center_lon]
                m.zoom_start = zoom
                if adjusted:
                    bounds = [[min_lat, min_lon], [max_lat, max_lon]]
                    bounds = [[b[0], (b[1] + 360) if b[1] < -180 else b[1]] for b in bounds]
                else:
                    bounds = [[min_lat, min_lon], [max_lat, max_lon]]
                try:
                    m.fit_bounds(bounds, padding=(50, 50))
                except Exception:
                    pass
            maps_dir = os.path.join("static", "maps")
            os.makedirs(maps_dir, exist_ok=True)
            map_path = os.path.join(maps_dir, "track_by_location.html")
            m.save(map_path)

            # render map view, preserve selected and search params for back link
            return render_template(
                "track_by_location.html",
                tracks=[],
                location=location,
                kmradius=kmradius,
                message=message,
                map_available=True,
                map_url="/" + map_path,
                selected=selected,
            )

        # otherwise treat as search
        if location:
            tracks, message = get_tracks_by_latlon(location, int(kmradius))
            # attach keywords for tracks
            track_names = [t.get('name') for t in tracks if t.get('name')]
            keywords_map = get_keywords_for_tracks(track_names)
            for t in tracks:
                t['keywords'] = keywords_map.get(t.get('name'), [])

    else:
        # GET - possibly coming from Back to list with query params
        location = request.values.get('location', '').strip() if request.values else ''
        kmradius = request.values.get('kmradius', kmradius) if request.values else kmradius
        selected = request.values.getlist('selected') if request.values else []
        if location:
            tracks, message = get_tracks_by_latlon(location, int(kmradius))
            track_names = [t.get('name') for t in tracks if t.get('name')]
            keywords_map = get_keywords_for_tracks(track_names)
            for t in tracks:
                t['keywords'] = keywords_map.get(t.get('name'), [])

    return render_template(
        "track_by_location.html",
        tracks=tracks,
        location=location,
        kmradius=kmradius,
        message=message,
        selected=selected,
        map_available=False,
    )


@app.route("/download/<path:filename>")
def download_gpx(filename):
    filename = unquote(filename)

    if not filename.endswith(".gpx"):
        abort(400)

    file_path = os.path.join(GPX_DIR, filename)
    if not os.path.isfile(file_path):
        abort(404)

    return send_from_directory(GPX_DIR, filename, as_attachment=True)


@app.route("/download_track/<int:year>/<int:month>/<int:day>/<path:filename>")
def download_track(year, month, day, filename):
    filename = unquote(filename)
    if not filename.endswith(".gpx"):
        abort(400)

    url = (
        f"{NEXTCLOUD_DAV_URL}"
        f"{NEXTCLOUD_TRACK_DIR}/"
        f"{year}/{month}/{day}/{filename}"
    )

    r = requests.get(url, auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD), stream=True, timeout=REQUEST_TIMEOUT)

    if r.status_code != 200:
        abort(404)

    response = Response(r.iter_content(chunk_size=8192))

    # 🔽 Force download in browser
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Type"] = r.headers.get(
        "Content-Type", "application/octet-stream"
    )

    return response


@app.route("/keywordsearch")
@login_required
def keywordsearch():
    keywords = []

    searchstring = request.args.get("searchstring", "").strip()
    mode = request.args.get("mode")
    if searchstring:
        keywords = get_keywords_by_searchstring(searchstring, mode)

    return render_template(
        "keywords.html", keywords=keywords, searchstring=searchstring, mode=mode
    )


@app.route("/datesearch")
@login_required
def datesearch():
    dates = []

    searchstring = request.args.get("searchstring", "").strip()
    mode = request.args.get("mode")
    if searchstring:
        dates = get_dates_by_searchstring(searchstring, mode)

    return render_template(
        "dates.html", dates=dates, searchstring=searchstring, mode=mode
    )


@app.route("/track_by_date")
@login_required
def track_by_date():
    date_str = request.args.get("date")  # e.g. "2025-01-15"
    tracks = []

    if date_str:
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        tracks = get_tracks_by_date(selected_date)

    return render_template("track_by_date.html", tracks=tracks, selected_date=date_str)


@app.route("/photos/<int:year>/<int:month>/<int:day>")
@login_required
def photos_by_date(year, month, day):
    url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, None)
    r = get_photos_request(url)

    if r.status_code not in (207, 200):
        url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, None)
        r = get_photos_request(url)
        if r.status_code not in (207, 200):
            return f"Nextcloud error: {r.status_code}", 500

    tree = ET.fromstring(r.text)
    ns = {"d": "DAV:"}

    photos = []
    for resp in tree.findall("d:response", ns):
        href = resp.find("d:href", ns).text
        if href.lower().endswith(".jpg"):
            filename = unquote(href.split("/")[-1])
            photos.append(filename)
            # logger.info("href=%r filename=%r", href, filename)

    return render_template(
        "photos.html",
        year=year,
        month=month,
        day=day,
        photos=photos,
    )


@app.route("/show_photo/<int:year>/<int:month>/<int:day>/<path:filename>")
@login_required
def show_photo(year, month, day, filename):
    url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, filename)
    r = get_show_photo_request(url)

    if r.status_code != 200:
        url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, filename)
        r = get_show_photo_request(url)
        if r.status_code != 200:
            abort(r.status_code)

    mimetype = r.headers.get("Content-Type")
    return Response(
        r.iter_content(chunk_size=8192),
        mimetype=mimetype or "image/jpeg",
    )


@app.route("/photo_info/<int:year>/<int:month>/<int:day>/<path:filename>")
@login_required
def photo_info(year, month, day, filename):
    photo = {}
    safe_filename = unquote(filename)

    url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, filename)
    r = get_photo_info_request(url)

    if r.status_code != 200:
        url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, filename)
        r = get_photo_info_request(url)
        if r.status_code != 200:
            abort(r.status_code)

    if get_photo_info(safe_filename, f"{year}-{month}-{day}"):
        row = get_photo_info(safe_filename, f"{year}-{month}-{day}")[0]
        """remove duplicate keywords"""
        keywords = []
        if row["keywords"]:
            keywords = sorted(
                set(kw.strip() for kw in row["keywords"].split(",") if kw.strip())
            )

        photo = {**row, "keywords": keywords}

    return render_template(
        "photo_info.html",
        year=year,
        month=month,
        day=day,
        filename=safe_filename,
        photo=photo,
    )


def get_tracks(cursor, cell_id, where_clause, params):
    result = ""
    sql = f"""
    SELECT
      t.date_taken,
      t.name, 
      t.type
    FROM track_h3 h
    JOIN track t
    ON h.track_name=t.name
    {where_clause}
    AND h.cell_id = '{cell_id}'
    """
    # logger.info(f"sql get tracks: {sql}")
    # logger.info(f"params get tracks: {params}")

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    for row in rows:
        result += f"<li><a target=\"_blank\" rel=\"noopener noreferrer\" href=\"/track_by_date?date={row['date_taken']}\">{row['date_taken']} - {row['name']} - {row['type']}</a></li>"
    return result


@app.route("/heatmap", methods=["GET", "POST"])
@login_required
def heatmap():
    filters = classes.HeatmapFilters()
    classes.get_bounding_box_for_location(filters)
    is_mobile = (
        request.user_agent.platform in ["android", "iphone"]
        or "Mobile" in request.user_agent.string
    )

    if request.method == "POST":
        action = request.form.get("action")

        if action == "clear_values":
            filters.clear_values()

            return render_template(
                "heatmap.html",
                filters=filters,
                limit_reached=False,
                result_rows=1,
                map_available=False,
            )

        filters.read_from_form(request.form)

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    basic_params = []
    visit_params = []
    where_clause = "WHERE 1=1 "
    having_clause = "HAVING visits >= %s"

    if filters.min_date:
        basic_params.append(filters.min_date)
        where_clause += " AND t.date_taken >= %s"
    if filters.max_date:
        basic_params.append(filters.max_date)
        where_clause += " AND t.date_taken <= %s"
    if filters.min_km is not None:
        basic_params.append(int(filters.min_km * 1000))
        where_clause += " AND t.length >= %s"
    if filters.max_km is not None:
        basic_params.append(int(filters.max_km * 1000))
        where_clause += " AND t.length <= %s"
    if filters.min_lat is not None:
        basic_params.append(filters.min_lat)
        where_clause += " AND t.middle_lat >= %s"
    if filters.max_lat is not None:
        basic_params.append(filters.max_lat)
        where_clause += " AND t.middle_lat <= %s"

    if filters.min_lon is not None:
        basic_params.append(filters.min_lon)
        where_clause += " AND t.middle_lon >= %s"
    if filters.max_lon is not None:
        basic_params.append(filters.max_lon)
        where_clause += " AND t.middle_lon <= %s"

    if filters.track_types:
        basic_params.extend(filters.track_types)
        placeholders = ",".join(["%s"] * len(filters.track_types))
        where_clause += " AND t.type IN (" + placeholders + ")"

    # there should always be min visits
    if filters.min_visits is None:
        filters.min_visits = 1
    visit_params.append(filters.min_visits)

    if filters.max_visits is not None:
        visit_params.append(filters.max_visits)
        having_clause += " AND visits <= %s"

    sql = f"""
    SELECT
    h.cell_id,
    t.type,
    t.name,
    t.date_taken,
    COUNT(*) AS visits

    FROM track_h3 h

    JOIN track t
    ON h.track_name=t.name
    
    {where_clause}

    GROUP BY h.cell_id
    {having_clause}
    LIMIT {filters.max_results + 1}
    """
    # logger.info(f"sql: {sql}")
    # logger.info(f"params: {basic_params + visit_params}")

    cursor.execute(sql, basic_params + visit_params)
    rows = cursor.fetchall()

    result_rows = len(rows)
    limit_reached = result_rows > filters.max_results
    if limit_reached:
        rows = rows[: filters.max_results]

    all_points = []

    m = folium.Map(tiles="OpenStreetMap")

    if rows:

        colormap = linear.Set1_09.to_step(9).scale(1, 10)

        for row in rows:
            cell_id = row["cell_id"]
            visits = row["visits"]

            boundary = h3.cell_to_boundary(cell_id)
            all_points.extend(boundary)
            color = colormap(visits)

            popup_text = get_tracks(cursor, cell_id, where_clause, basic_params)
            popup = folium.Popup(popup_text, max_width=300)

            polygon = folium.Polygon(
                locations=boundary,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.6,
                weight=0,
                popup=popup,
            )

            if not is_mobile:
                polygon.add_child(
                    folium.Tooltip(
                        f"{visits} visits<br /> <ul>{get_tracks(cursor, cell_id, where_clause, basic_params)}</ul>"
                    )
                )

            polygon.add_to(m)

        colormap.caption = "Number of visits"
        colormap.add_to(m)
        m.fit_bounds(all_points)
    m.save("static/maps/heatmap.html")

    cursor.close()
    conn.close()

    return render_template(
        "heatmap.html",
        map_available=True,
        filters=filters,
        limit_reached=limit_reached,
        result_rows=result_rows,
    )


def get_show_photo_request(url: str) -> Response:
    r = requests.get(
        url,
        auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
        stream=True,
        timeout=REQUEST_TIMEOUT,
    )
    return r


def get_photo_info_request(url: str) -> Response:
    r = requests.get(
        url,
        auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
        verify=True,  # now properly fixed
        timeout=REQUEST_TIMEOUT,
    )
    return r


def get_nextcloud_url(dir, year, month, day, filename) -> str:
    base_path = f"{dir}/{year}/{month}/{day}"
    if filename:
        base_path = f"{base_path}/{filename}"
    url = NEXTCLOUD_DAV_URL + base_path
    return url


def get_photos_request(url: str) -> Response:
    headers = {"Depth": "1"}
    r = requests.request(
        "PROPFIND",
        url,
        headers=headers,
        auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
    )
    return r


# if __name__ == "__main__":
#     app.run(debug=True)

from flask import Flask, render_template, request, redirect, abort, session, send_file, g, jsonify
from flask_socketio import SocketIO, send, emit, join_room, leave_room
from flask_mobility import Mobility
from getters import *
from fast_download import fast_download, get_path
from batch_download import BatchDownloadManager, BatchQueueFullError, validate_batch_selection
from shikimori_metadata import build_mp4_metadata, fetch_shikimori_metadata
import watch_together
from json import load
import config
import os

app = Flask(__name__)
Mobility(app)
socketio = SocketIO(app)

token = config.KODIK_TOKEN
app.config['SECRET_KEY'] = config.APP_SECRET_KEY

with open("translations.json", 'r', encoding='utf-8') as f:
    # Используется для указания озвучки при скачивании файла
    translations = load(f)

if config.USE_SAVED_DATA or config.SAVE_DATA:
    from cache import Cache
    ch = Cache(config.SAVED_DATA_FILE, config.SAVING_PERIOD, config.CACHE_LIFE_TIME)
ch_save = config.SAVE_DATA
ch_use = config.USE_SAVED_DATA

watch_manager = watch_together.Manager(config.REMOVE_TIME)
batch_download_manager = BatchDownloadManager(config.ANIME_DIRECTORY)

# Проверка доступности шикимори
test_shiki()


@app.route('/')
def index():
    return render_template('index.html', is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_kodik_search=USE_KODIK_SEARCH)

@app.route('/', methods=['POST'])
def index_form():
    data = dict(request.form)
    if 'shikimori_id' in data.keys():
        return redirect(f"/download/sh/{data['shikimori_id']}/")
    if 'kinopoisk_id' in data.keys():
        return redirect(f"/download/kp/{data['kinopoisk_id']}/")
    elif 'kdk' in data.keys(): # kdk = Kodik
        return redirect(f"/search/kdk/{data['kdk']}/")
    else:
        return abort(400)
    
@app.route("/change_theme/", methods=['POST'])
def change_theme():
    # Костыль для смены темы
    if "is_dark" in session.keys():
        session['is_dark'] = not(session['is_dark'])
    else:
        session['is_dark'] = True
    return redirect(request.referrer)

@app.route('/search/<string:db>/<string:query>/')
def search_page(db, query):
    if db == "kdk":
        try:
            # Попытка получить данные с кодика
            s_data = get_search_data(query, token, ch if ch_save or ch_use else None)
            return render_template('search.html', items=s_data[0], others=s_data[1], is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile, is_kodik_search=USE_KODIK_SEARCH)
        except requests.exceptions.SSLError:
            return abort(500, 'Произошла ошибка подключения при получении данных с шикимори. Проверьте доступность сайта и правильность указанного зеркала и прокси!')
        except:
            return render_template('search.html', is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile, is_kodik_search=USE_KODIK_SEARCH)
    else:
        # Другие базы не поддерживаются (возможно в будущем будут)
        return abort(400)

@app.route('/download/<string:serv>/<string:id>/')
def download_shiki_choose_translation(serv, id):
    if serv == "sh":
        if ch_use and ch.is_id("sh"+id) and ch.get_data_by_id("sh"+id)['serial_data'] != {}:
            serial_data = ch.get_data_by_id("sh"+id)['serial_data']
        else:
            try:
                # Получаем данные о наличии переводов от кодика
                serial_data = get_serial_info(id, "shikimori", token)
            except Exception as ex:
                return f"""
                <h1>По данному запросу нет данных</h1>
                {f'<p>Exception type: {ex}</p>' if config.DEBUG else ''}
                """
        cache_used = False
        if ch_use and ch.is_id("sh"+id):
            # Проверка кеша на наличие данных
            cached = ch.get_data_by_id("sh"+id)
            name = cached['title']
            pic = cached['image']
            score = cached['score']
            dtype = cached['type']
            date = cached['date']
            status = cached['status']
            rating = cached['rating']
            year = cached['year']
            description = cached['description']
            if is_good_quality_image(pic):
                # Проверка что была сохранена картинка в полном качестве
                # (При поиске по шики, выдаются картинки в урезанном качестве)
                cache_used = True
        if not cache_used:
            try:
                # Попытка получить данные с шики
                data = get_shiki_data(id)
                name = data['title']
                pic = data['image']
                score = data['score']
                dtype = data['type']
                date = data['date']
                status = data['status']
                rating = data['rating']
                year = data['year']
                description = data['description']
            except:
                name = 'Неизвестно'
                pic = config.IMAGE_NOT_FOUND
                score = 'Неизвестно'
                dtype = 'Неизвестно'
                date = 'Неизвестно'
                status = 'Неизвестно'
                rating = 'Неизвестно'
                year = 'Неизвестно'
                description = 'Неизвестно'
                data = False
            finally:
                if ch_save and not ch.is_id("sh"+id):
                    # Записываем данные в кеш если их там нет
                    ch.add_id("sh"+id, name, pic, score, data['status'] if data else "Неизвестно", 
                              data['date'] if data else "Неизвестно", data['year'] if data else 1970, 
                              data['type'] if data else "Неизвестно", data['rating'] if data else "Неизвестно", 
                              data['description'] if data else '', serial_data=serial_data)
        if ch_use and ch_save and ch.is_id("sh"+id) and ch.get_data_by_id("sh"+id)['serial_data'] == {}:
            ch.add_serial_data("sh"+id, serial_data)
        try:
            if ch_use and ch.is_id("sh"+id) and ch.get_data_by_id("sh"+id)['related'] != []:
                related = ch.get_data_by_id("sh"+id)['related']
            else:
                related = get_related(id, 'shikimori', sequel_first=True)
                ch.add_related("sh"+id, related)
        except:
            related = []
        return render_template('info.html', 
            title=name, image=pic, score=score, translations=serial_data['translations'], series_count=serial_data["series_count"], id=id, 
            dtype=dtype, date=date, status=status, rating=rating, related=related, description=description, is_shiki=True,
            is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile,
            shiki_mirror=config.SHIKIMORI_MIRROR if config.SHIKIMORI_MIRROR else "shikimori.one")
    elif serv == "kp":
        try:
            # Получаем данные о наличии переводов от кодика
            serial_data = get_serial_info(id, "kinopoisk", token)
        except Exception as ex:
            return f"""
            <h1>По данному запросу нет данных</h1>
            {f'<p>Exception type: {ex}</p>' if config.DEBUG else ''}
            """
        return render_template('info.html', 
            title="...", image=config.IMAGE_NOT_FOUND, score="...", translations=serial_data['translations'], series_count=serial_data["series_count"], id=id, 
            dtype="...", date="...", status="...", description='...', is_shiki=False,
            is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile)
    else:
        return abort(400)

@app.route('/download/<string:serv>/<string:id>/<string:data>/')
def download_choose_seria(serv, id, data):
    if data == "None":
        return
    data = data.split('-')
    if data[0].split(':')[1] == '0':
        series = 0
    else:
        series = [int(x) for x in data[0].split(":")]
    translation_id = str(data[1])
    return render_template('download.html', series=series, backlink=f"/download/{serv}/{id}/",
                           serv=serv, anime_id=id, translation_id=translation_id,
                           is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile)

def _get_batch_serial_data(serv, anime_id):
    if serv not in {"sh", "kp"}:
        raise ValueError("Неизвестный источник аниме")
    id_type = "shikimori" if serv == "sh" else "kinopoisk"
    return get_serial_info(anime_id, id_type, token)


def _get_download_media_metadata(serv, anime_id, episode, translation_name):
    if serv != "sh":
        return {}
    shikimori_data = fetch_shikimori_metadata(anime_id)
    return build_mp4_metadata(
        shikimori_data,
        anime_id=anime_id,
        episode=episode,
        translation=translation_name,
    )


@app.route('/batch_download/start/', methods=['POST'])
def start_batch_download():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='Ожидается JSON-объект'), 400
    required = ('serv', 'anime_id', 'translation_id', 'quality', 'first_episode', 'last_episode')
    missing = [field for field in required if field not in payload]
    if missing:
        return jsonify(error='Отсутствуют поля: '+', '.join(missing)), 400
    try:
        if type(payload['first_episode']) is not int or type(payload['last_episode']) is not int:
            raise TypeError('Номера серий должны быть целыми числами')
        for field in ('serv', 'anime_id', 'translation_id', 'quality'):
            if not isinstance(payload[field], str) or not payload[field].strip():
                raise TypeError(f'Поле {field} должно быть непустой строкой')
        first_episode = payload['first_episode']
        last_episode = payload['last_episode']
        if first_episode < 0 or last_episode < first_episode:
            raise ValueError('Некорректный диапазон серий')
        if last_episode - first_episode + 1 > config.MAX_BATCH_EPISODES:
            raise ValueError(
                f'За один раз можно поставить в очередь не более {config.MAX_BATCH_EPISODES} серий'
            )
        serv = payload['serv']
        anime_id = payload['anime_id']
        translation_id = payload['translation_id']
        quality = payload['quality']
        try:
            serial_data = _get_batch_serial_data(serv, anime_id)
        except ValueError:
            raise
        except Exception:
            return jsonify(error='Не удалось проверить данные аниме на сервере'), 502
        translation = validate_batch_selection(
            serial_data,
            translation_id,
            first_episode,
            last_episode,
        )
        translation_name = translation.get('name') or translations.get(translation_id, 'Неизвестно')
        try:
            media_metadata = _get_download_media_metadata(
                serv,
                anime_id,
                0,
                translation_name,
            )
        except Exception:
            return jsonify(error='Не удалось получить метаданные Shikimori'), 502
        anime_title = None
        if ch_use:
            try:
                cached = ch.get_data_by_id(serv+anime_id)
                anime_title = cached['title'] if cached else None
            except (KeyError, TypeError):
                anime_title = None
        job_id = batch_download_manager.start_job(
            serv=serv,
            anime_id=anime_id,
            translation_id=translation_id,
            translation_name=translation_name,
            quality=quality,
            episodes=range(first_episode, last_episode+1),
            anime_title=anime_title,
            token=token,
            media_metadata=media_metadata,
        )
        return jsonify(batch_download_manager.get_status(job_id)), 202
    except BatchQueueFullError as ex:
        return jsonify(error=str(ex)), 429
    except (TypeError, ValueError) as ex:
        return jsonify(error=str(ex)), 400

@app.route('/batch_download/status/<string:job_id>/')
def batch_download_status(job_id):
    try:
        return jsonify(batch_download_manager.get_status(job_id))
    except KeyError:
        return jsonify(error='Задание не найдено'), 404

@app.route('/download/<string:serv>/<string:id>/<string:data>/<string:download_type>-<string:quality>-<int:seria>/')
def redirect_to_download(serv, id, data, download_type, quality, seria):
    data = data.split('-')
    series = [int(x) for x in data[0].split(":")]
    translation_id = str(data[1])
    if download_type == 'fast':
        return redirect(f'/fast_download/{serv}-{id}-{seria}-{translation_id}-{quality}-{series[1]}/')
    try:
        if serv == "sh":
            if ch_use and ch.is_seria("sh"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("sh"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "shikimori", seria, translation_id, token)
                if ch_save:
                    # Записываем данные в кеш
                    try:
                        # Попытка записать данные к уже имеющимся данным
                        ch.add_seria("sh"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        elif serv == "kp":
            if ch_use and ch.is_seria("kp"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("kp"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "kinopoisk", seria, translation_id, token)
                if ch_save:
                    # Записываем данные в кеш
                    try:
                        # Попытка записать данные к уже имеющимся данным
                        ch.add_seria("kp"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        else:
            return abort(400)
        url = url[0] # Берем только ссылку (тут хранятся еще и качество и сегменты для пропуска)
        translation = translations[translation_id] if translation_id in translations else "Неизвестно"
        if seria == 0:
            return redirect(f"https:{url}{quality}.mp4:Перевод-{translation}:.mp4")
        else:
            return redirect(f"https:{url}{quality}.mp4:Серия-{seria}:Перевод-{translation}:.mp4")
    except Exception as ex:
        return abort(500, f'Exception: {ex}')

@app.route('/download/<string:serv>/<string:id>/<string:data>/watch-<int:num>/')
def redirect_to_player(serv, id, data, num):
    series = [int(x) for x in data.split("-")[0].split(':')]
    if series[0] == 0 and series[1] == 0:
        return redirect(f'/watch/{serv}/{id}/{data}/0/')
    else:
        return redirect(f'/watch/{serv}/{id}/{data}/{num}/')

@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:old_quality>/q-<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:old_quality>/<int:timing>/q-<string:quality>/')
def change_watch_quality(serv, id, data, seria, old_quality, quality, timing = None):
    return redirect(f"/watch/{serv}/{id}/{data}/{seria}/{quality}/{str(timing)+'/' if timing else ''}")

@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/q-<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/q-<string:quality>/<int:timing>/')
def redirect_to_old_type_quality(serv, id, data, seria, quality, timing = 0):
    return redirect(f"/watch/{serv}/{id}/{data}/{seria}/{quality}/{str(timing)+'/' if timing else ''}")

@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/<int:timing>/')
def watch(serv, id, data, seria, quality = "720", timing = 0):
    try:
        data = data.split('-')
        series = [int(x) for x in data[0].split(":")]
        translation_id = str(data[1])
        title = None
        if serv == "sh":
            id_type = "shikimori"
            if ch_use:
                try:
                    title = ch.get_data_by_id("sh"+id)['title'] if ch.get_data_by_id("sh"+id) else None
                except:
                    title = None
            if ch_use and ch.is_seria("sh"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("sh"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "shikimori", seria, translation_id, token)
                if ch_save and not ch.is_seria("sh"+id, translation_id, seria):
                    # Записываем данные в кеш
                    try:
                        ch.add_seria("sh"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        elif serv == "kp":
            id_type = "kinopoisk"
            if ch_use:
                try:
                    title = ch.get_data_by_id("kp"+id)['title'] if ch.get_data_by_id("kp"+id) else None
                except:
                    title = None
            if ch_use and ch.is_seria("kp"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("kp"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "kinopoisk", seria, translation_id, token)
                if ch_save and not ch.is_seria("kp"+id, translation_id, seria):
                    # Записываем данные в кеш
                    try:
                        ch.add_seria("kp"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        else:
            return abort(400)
        skip_segments = url[2]
        url = url[0] # Берем только ссылку (тут хранятся еще и качество и сегменты для пропуска)
        straight_url = f"https:{url}{quality}.mp4" # Прямая ссылка
        url = f"/download/{serv}/{id}/{'-'.join(data)}/old-{quality}-{seria}" # Ссылка на скачивание через этот сервер
        return render_template('watch.html',
            url=url, seria=seria, series=series, id=id, id_type=id_type, data="-".join(data), quality=quality, serv=serv, straight_url=straight_url,
            allow_watch_together=config.ALLOW_WATCH_TOGETHER,
            is_dark=session['is_dark'] if "is_dark" in session.keys() else False,
            timing=timing, title=title, skip_segments=skip_segments, is_mobile=g.is_mobile)
    except:
        return abort(404)

@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/', methods=['POST'])
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/', methods=['POST'])
def change_seria(serv, id, data, seria, quality=None):
    # Если использовалась форма для изменения серии
    try:
        new_seria = int(dict(request.form)['seria'])
    except:
        return abort(400)
    data = data.split('-')
    series = int(data[0])
    if new_seria > series or new_seria < 1:
        return abort(400, "Данная серия не существует")
    else:
        return redirect(f"/watch/{serv}/{id}/{'-'.join(data)}/{new_seria}{'/'+quality if quality != None else ''}")
    

# Watch Together ===================================================
@app.route('/create_room/', methods=['POST'])
def create_room():
    orig = request.referrer
    data = orig.split("/")
    if len(data) == 9:
        data[8] = 720
        data.append('')
    temp = data[-4].split('-')
    data = {
        'serv': data[-6],
        'id': data[-5],
        'series_count': int(temp[0].split(':')[1]),
        'translation_id': temp[1],
        'seria': int(data[-3]),
        'quality': int(data[-2]),
        'pause': False,
        'play_time': 0,
    }
    rid = watch_manager.new_room(data)
    watch_manager.remove_old_rooms()
    return redirect(f"/room/{rid}/")

@app.route('/room/<string:rid>/', methods=["GET"])
def room(rid):
    if not watch_manager.is_room(rid):
        return abort(404)
    rd = watch_manager.get_room_data(rid)
    watch_manager.room_used(rid)
    try:
        id = rd['id']
        seria = rd['seria']
        series = rd['series_count']
        translation_id = str(rd['translation_id'])
        quality = rd['quality']
        if rd['serv'] == "sh":
            id_type = "shikimori"
            if ch_use and ch.is_seria("sh"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("sh"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "shikimori", seria, translation_id, token)
                if ch_save and not ch.is_seria("sh"+id, translation_id, seria):
                    # Записываем данные в кеш
                    try:
                        ch.add_seria("sh"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        elif rd['serv'] == "kp":
            id_type = "kinopoisk"
            if ch_use and ch.is_seria("kp"+id, translation_id, seria):
                # Получаем данные из кеша (если есть и используется)
                url = ch.get_seria("kp"+id, translation_id, seria)
            else:
                # Получаем данные с сервера
                url = get_download_link(id, "kinopoisk", seria, translation_id, token)
                if ch_save and not ch.is_seria("kp"+id, translation_id, seria):
                    # Записываем данные в кеш
                    try:
                        ch.add_seria("kp"+id, translation_id, seria, url[0], url[2])
                    except KeyError:
                        pass
        else:
            return abort(400)
        url = url[0] # Берем только ссылку (тут хранятся еще и качество и сегменты для пропуска)
        straight_url = f"https:{url}{quality}.mp4" # Прямая ссылка
        url = f"/download/{rd['serv']}/{id}/{series}-{translation_id}/{quality}-{seria}" # Ссылка на скачивание через этот сервер
        return render_template('room.html',
            url=url, seria=seria, series=series, id=id, id_type=id_type, data=f"{series}-{translation_id}", quality=quality, serv=rd['serv'], straight_url=straight_url,
            start_time=rd['play_time'],
            is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile)
    except:
        return abort(500)

@app.route('/room/<string:rid>/', methods=["POST"])
def change_room_seria_form(rid):
    data = dict(request.form)['seria']
    rdata = watch_manager.get_room_data(rid)
    if data == '':
        pass
    rdata['seria'] = int(data)
    rdata['play_time'] = 0
    watch_manager.room_used(rid)
    socketio.send({"data": {"status": 'update_page', 'time': 0}}, to=rid)
    return redirect(f"/room/{rid}/")

@app.route('/room/<string:rid>/cs-<int:seria>/')
def change_room_seria(rid, seria):
    if not watch_manager.is_room(rid):
        return abort(400)
    rdata = watch_manager.get_room_data(rid)
    rdata['seria'] = seria
    rdata['play_time'] = 0
    watch_manager.room_used(rid)
    socketio.send({"data": {"status": 'update_page', 'time': 0}}, to=rid)
    return redirect(f"/room/{rid}/")

@app.route('/room/<string:rid>/cq-<int:quality>/')
def change_room_quality(rid, quality):
    if not watch_manager.is_room(rid):
        return abort(400)
    rdata = watch_manager.get_room_data(rid)
    rdata['quality'] = quality
    watch_manager.room_used(rid)
    socketio.send({"data": {"status": 'update_page', 'time': rdata['play_time']}}, to=rid)
    return redirect(f"/room/{rid}/")

@app.route('/fast_download_act/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>/')
@app.route('/fast_download_act/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>-<int:max_series>/')
def fast_download_work(id_type: str, id: str, seria_num: int, translation_id: str, quality: str, max_series: int = 12):
    from fast_download import fast_download_open
    translation = translations[translation_id] if translation_id in translations else "Неизвестно"
    add_zeros = len(str(max_series))
    if config.USE_SAVED_DATA and ch.is_id(id_type+id):
        if seria_num != 0:
            fname = str(ch.get_data_by_id(id_type+id)['title'])+'-'+f'Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p'
        else:
            fname = str(ch.get_data_by_id(id_type+id)['title'])+'-'+f'Перевод-{translation}-{quality}p'
    else:
        fname = f'Перевод-{translation}-{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p'
    try:
        metadata = _get_download_media_metadata(id_type, id, seria_num, translation)
    except Exception:
        metadata = {}
    if len(fname) > 128: # Ограничение на длину имени файла в винде 255 символов, в линуксе 255 байт (т.е. для кириллицы 128 символов)
        if len(translation) > 100:
            fname = f'{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-{quality}p'
        else:
            fname = f'Перевод-{translation}-{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p'
    # Чистка имени файла от запрещенных символов
    # +=[]:*?;«,./\<>|'пробел'  /\:*?<>|
    fname = fname.replace('\\','-').replace('/', '-').replace(':', '-').replace('*','-').replace('"', '\'') \
        .replace('»', '\'').replace('«', '\'').replace('„', '\'').replace('“', '\'').replace('<', '[') \
        .replace(']', ')').replace('|', '-').replace('--', '-').replace('--', '-')
    try:
        _hsh, link_data, source = fast_download_open(id, id_type, seria_num, translation_id, quality, config.KODIK_TOKEN,
                            filename=fname, metadata=metadata)
        try:
            if ch_save and link_data is not None:
                try:
                    # Попытка записать данные к уже имеющимся данным
                    ch.add_seria(
                        id_type+id,
                        translation_id,
                        seria_num,
                        link_data[0],
                        link_data[2],
                    )
                except KeyError:
                    pass
            return send_file(source, as_attachment=True, download_name=fname+'.mp4')
        except Exception:
            source.close()
            raise
    except ModuleNotFoundError:
        return abort(500, 'Внимание, на сервере не установлен ffmpeg или программа не может получить к нему доступ. Ffmpeg обязателен для использования быстрой загрузки. (Стандартная загрузка работает без ffmpeg)')
    except FileNotFoundError:
        return abort(404, 'Видеофайл не найден, попробуйте сменить качество')

@app.route('/fast_download/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>/')
@app.route('/fast_download/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>-<int:max_series>/')
def fast_download_prepare(id_type: str, id: str, seria_num: int, translation_id: str, quality: str, max_series: int = 12):
    return render_template('fast_download_prepare.html', seria_num=seria_num,
                           url=f'/fast_download_act/{id_type}-{id}-{seria_num}-{translation_id}-{quality}-{max_series}/',
                           past_url=request.referrer if request.referrer else f'/download/{id_type}/{id}/',
                           is_dark=session['is_dark'] if "is_dark" in session.keys() else False, is_mobile=g.is_mobile)

# =======================================================================
# Sockets ====================================

@socketio.on('join')
def on_join(data):
    join_room(data['rid'])
    if not watch_manager.is_room(data['rid']):
        pass
    watch_manager.room_used(data['rid'])
    return send({'data': {'status': 'loading', 'time': watch_manager.get_room_data(data['rid'])['play_time']}}, to=data['rid'])

@socketio.on('broadcast')
def broadcast(data):
    watch_manager.room_used(data['rid'])
    watch_manager.update_play_time(data['rid'], data['data']['time'])
    return send(data, to=data['rid'])

#  ===========================================
# Shortcuts vvvv

@app.route('/help/')
def help():
    # Заглушка
    return redirect("https://github.com/YaNesyTortiK/Kodik-Download-Watch/blob/main/README.MD")

@app.route('/resources/<string:path>')
def resources(path: str):
    if os.path.exists(f'resources\\{path}'): # Windows-like
        return send_file(f'resources\\{path}')
    elif os.path.exists(f'resources/{path}'): # Unix
        return send_file(f'resources/{path}')
    else:
        return abort(404)

@app.route('/favicon.ico')
def favicon():
    return send_file(config.FAVICON_PATH)

if __name__ == "__main__":
    socketio.run(app, host=config.HOST, port=config.PORT, debug=config.DEBUG)

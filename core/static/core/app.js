(function () {
    'use strict';

    const STEPS = ['upload', 'processing', 'result'];
    // как часто спрашивать сервер о ходе обработки
    const POLL_MS = 1500;
    const LEAVE_MS = 900;
    const SLOT_MS = 600;
    const FADE_MS = 450;
    const SETTLE_MS = 180;

    const previewUrl = document.body.dataset.previewUrl;
    // PREVIEW_ON_UPLOAD: без превью файл на этапе загрузки только выбирается, никуда не уходит
    const previewOnUpload = document.body.dataset.previewUpload === '1';
    const form = document.getElementById('form');
    const submitButton = document.getElementById('submit');
    const errorLine = document.getElementById('error');
    const downloadLink = document.getElementById('download');
    const planLink = document.getElementById('download-plan');
    const restartButton = document.getElementById('restart');
    const viewerButton = document.getElementById('open-viewer');
    const summaryList = document.getElementById('result-summary');
    const stageLine = document.getElementById('stage-line');
    const stageItems = document.querySelectorAll('#stage-list [data-stage]');
    const jobsUrl = document.body.dataset.jobsUrl;

    const panels = {};
    const marks = {};
    document.querySelectorAll('[data-step]').forEach(function (el) { panels[el.dataset.step] = el; });
    document.querySelectorAll('[data-for]').forEach(function (el) { marks[el.dataset.for] = el; });

    const resultName = panels.result.querySelector('.drop-name');
    const resultMeta = panels.result.querySelector('.drop-meta');

    let currentStep = 'upload';
    // текущая задача: номер, имя файла, сводка и данные просмотра (загружаются при первом открытии)
    let job = null;

    function goTo(step) {
        if (step === currentStep) return;

        const from = panels[currentStep];
        from.classList.remove('is-active');
        from.classList.add('is-leaving');

        let settled = false;

        function settle() {
            if (settled) return;
            settled = true;
            from.removeEventListener('transitionend', onEnd);
            from.classList.add('no-anim');
            from.classList.remove('is-leaving');
            void from.offsetWidth;
            from.classList.remove('no-anim');
        }

        // transitionend всплывает и от вложенных элементов
        function onEnd(event) {
            if (event.target === from && event.propertyName === 'opacity') settle();
        }

        from.addEventListener('transitionend', onEnd);
        setTimeout(settle, LEAVE_MS);

        panels[step].classList.add('is-active');
        currentStep = step;

        const index = STEPS.indexOf(step);
        STEPS.forEach(function (name, i) {
            marks[name].classList.toggle('is-active', i === index);
            marks[name].classList.toggle('is-done', i < index);
        });
    }

    function panZoom(view) {
        let scale = 1;
        let x = 0;
        let y = 0;
        let drag = null;
        let frame = 0;
        let settleTimer = null;

        function paint() {
            frame = 0;
            const svg = view.firstElementChild;
            if (svg) svg.style.transform = 'translate(' + x + 'px, ' + y + 'px) scale(' + scale + ')';
        }

        function settle() {
            clearTimeout(settleTimer);
            settleTimer = setTimeout(function () {
                if (!drag) view.classList.remove('is-moving');
            }, SETTLE_MS);
        }

        // Большой чертёж — сотни тысяч путей. Во время жеста слой двигается как готовая
        // картинка (is-moving → will-change), не чаще раза за кадр; чётко перерисовывается
        // один раз, когда жест закончился.
        function apply() {
            view.classList.add('is-moving');
            if (!frame) frame = requestAnimationFrame(paint);
            settle();
        }

        view.addEventListener('wheel', function (e) {
            if (!view.firstElementChild) return;
            e.preventDefault();
            const box = view.getBoundingClientRect();
            const px = e.clientX - box.left;
            const py = e.clientY - box.top;
            const next = Math.min(Math.max(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 0.1), 60);
            const k = next / scale;
            x = px - (px - x) * k;
            y = py - (py - y) * k;
            scale = next;
            apply();
        }, { passive: false });

        view.addEventListener('pointerdown', function (e) {
            if (!view.firstElementChild) return;
            drag = { x: e.clientX, y: e.clientY };
            view.setPointerCapture(e.pointerId);
        });

        view.addEventListener('pointermove', function (e) {
            if (!drag) return;
            x += e.clientX - drag.x;
            y += e.clientY - drag.y;
            drag = { x: e.clientX, y: e.clientY };
            apply();
        });

        ['pointerup', 'pointercancel'].forEach(function (type) {
            view.addEventListener(type, function () {
                drag = null;
                settle();
            });
        });

        function reset() {
            cancelAnimationFrame(frame);
            clearTimeout(settleTimer);
            view.classList.remove('is-moving');
            scale = 1;
            x = 0;
            y = 0;
        }

        return {
            show: function (svg) {
                reset();
                view.innerHTML = svg;
                paint();
            },
            clear: function () {
                reset();
                view.innerHTML = '';
            },
        };
    }

    function previewPanel(section) {
        const toggle = section.querySelector('.preview-toggle');
        const label = toggle.querySelector('.preview-toggle-label');
        const slot = section.querySelector('.preview-slot');
        const box = slot.querySelector('.preview');
        const note = box.querySelector('.preview-note');
        const view = panZoom(box.querySelector('.preview-view'));

        let timer = null;
        let opened = false;
        let data = null;
        // растёт при каждом открытии, закрытии и сбросе: запоздавший разбор ответа не должен
        // раскрыть превью, которое уже закрыли или сменили
        let generation = 0;

        function setNote(text) {
            note.textContent = text;
            note.hidden = !text;
        }

        // Ответ превью приходит Blob'ом: браузер держит его вне памяти JS, пока превью не открыли.
        // Разбираем при первом открытии и запоминаем результат.
        function parsed() {
            if (!(data instanceof Blob)) return Promise.resolve(data);
            const blob = data;
            return blob.text().then(function (text) {
                const next = JSON.parse(text);
                if (data === blob) data = next;
                return next;
            }).catch(function () { return {}; });
        }

        // раскрытие: сначала разбор ответа (чтобы не дёрнуть анимацию), затем слот набирает
        // высоту, затем проявляется чертёж
        function open() {
            clearTimeout(timer);
            opened = true;
            toggle.classList.add('is-open');
            label.textContent = 'Скрыть';

            const current = ++generation;
            parsed().then(function (ready) {
                if (current !== generation) return;

                if (ready.svg) {
                    setNote('');
                    view.show(ready.svg);
                } else {
                    view.clear();
                    setNote(ready.note || 'Превью недоступно');
                }

                box.hidden = false;
                slot.classList.add('is-open');
                timer = setTimeout(function () { box.classList.add('is-ready'); }, SLOT_MS);
            });
        }

        function close() {
            clearTimeout(timer);
            generation++;
            opened = false;
            toggle.classList.remove('is-open');
            label.textContent = 'Обзор';
            box.classList.remove('is-ready');

            timer = setTimeout(function () {
                slot.classList.remove('is-open');
                timer = setTimeout(function () {
                    box.hidden = true;
                    view.clear();
                }, SLOT_MS);
            }, FADE_MS);
        }

        toggle.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (!data) return;
            if (opened) close(); else open();
        });

        return {
            load: function (next) {
                data = next;
                toggle.disabled = false;
            },
            reset: function () {
                clearTimeout(timer);
                generation++;
                data = null;
                opened = false;
                toggle.disabled = true;
                toggle.classList.remove('is-open');
                label.textContent = 'Обзор';
                slot.classList.remove('is-open');
                box.classList.remove('is-ready');
                box.hidden = true;
                setNote('');
                view.clear();
            },
        };
    }

    // превью на этапе загрузки выключено: панели и процента нет, а ход отправки нечему считать
    const NO_PREVIEW = { load: function () {}, reset: function () {} };
    const NO_PROGRESS = { upload: function () {}, stop: function () {} };

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' Б';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' КБ';
        return (bytes / (1024 * 1024)).toFixed(1) + ' МБ';
    }

    // XHR вместо fetch: только он сообщает о ходе отправки файла.
    // lazy: удачный ответ отдаётся Blob'ом без разбора (см. parsed в previewPanel).
    function post(url, files, onProgress, lazy) {
        const body = new FormData();
        body.append('csrfmiddlewaretoken', form.elements.csrfmiddlewaretoken.value);
        Object.keys(files).forEach(function (name) { body.append(name, files[name]); });

        let xhr = null;
        const request = new Promise(function (resolve, reject) {
            xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.responseType = lazy ? 'blob' : 'text';

            if (onProgress) {
                xhr.upload.addEventListener('progress', function (e) {
                    if (e.lengthComputable) onProgress(e.loaded / e.total);
                });
                xhr.upload.addEventListener('load', function () { onProgress(1); });
            }

            xhr.addEventListener('load', function () {
                const ok = xhr.status >= 200 && xhr.status < 300;
                if (ok && lazy) {
                    resolve(xhr.response);
                    return;
                }

                // ответ с ошибкой маленький — разбираем сразу, чтобы показать сообщение
                const text = lazy ? xhr.response.text() : Promise.resolve(xhr.responseText);
                text.then(function (body) {
                    let data = null;
                    try { data = JSON.parse(body); } catch (err) { /* не JSON */ }

                    if (ok) {
                        resolve(data);
                        return;
                    }
                    const messages = data && data.errors ? Object.values(data.errors).flat() : [];
                    reject(new Error(messages.join(' ') || 'Не удалось обработать файл.'));
                });
            });

            xhr.addEventListener('error', function () {
                reject(new Error('Не удалось отправить файл.'));
            });

            xhr.addEventListener('abort', function () {
                reject(new Error('Отправка отменена.'));
            });

            xhr.send(body);
        });

        // выбрали другой файл — незачем догружать прежние 100 МБ
        request.abort = function () { xhr.abort(); };
        return request;
    }

    const BINARY_SENTINEL = 'AutoCAD Binary DXF';

    // Та же проверка, что в validators.py, но до отправки: не гоним весь файл ради отказа.
    // Читаем только первые 1024 байта.
    function checkDxf(file) {
        if (!/\.dxf$/i.test(file.name)) return Promise.resolve('Ожидается файл с расширением .dxf.');

        return file.slice(0, 1024).arrayBuffer().then(function (buffer) {
            const head = new TextDecoder().decode(buffer);
            if (head.startsWith(BINARY_SENTINEL)) return '';

            const text = head.replace(/^\s+/, '');
            const code = text.split(/\s/, 1)[0];
            const looksLikeDxf = (code === '0' || code === '999') && text.indexOf('SECTION') !== -1;
            return looksLikeDxf ? '' : 'Содержимое файла не похоже на DXF.';
        });
    }

    // Отправка файла — реальные 0–30%. Сколько сервер разбирает чертёж, он не сообщает,
    // поэтому 30–95% — оценка по размеру файла (замер на 100 МБ: ~0,7 с на МБ), 100% — пришёл ответ.
    const UPLOAD_SHARE = 0.3;
    const ESTIMATE_CAP = 0.95;

    function progressTracker(file, setPercent) {
        const expected = 300 + (file.size / (1024 * 1024)) * 700;
        let shown = 0;
        let uploaded = 0;
        let serverStart = null;

        function tick() {
            let value = uploaded * UPLOAD_SHARE;
            if (serverStart !== null) {
                const t = performance.now() - serverStart;
                value = UPLOAD_SHARE + (ESTIMATE_CAP - UPLOAD_SHARE) * (1 - Math.exp(-2 * t / expected));
            }
            shown = Math.max(shown, value);
            setPercent(shown);
        }

        const timer = setInterval(tick, 100);
        setPercent(0);

        return {
            upload: function (fraction) {
                uploaded = fraction;
                if (fraction >= 1 && serverStart === null) serverStart = performance.now();
                tick();
            },
            stop: function () { clearInterval(timer); },
        };
    }

    let submitError = '';

    // «Файл геоподосновы: не удалось…», но аббревиатуры вроде DXF не трогаем
    function withTitle(title, message) {
        const second = message.charAt(1);
        const first = second && second === second.toLowerCase() ? message.charAt(0).toLowerCase() : message.charAt(0);
        return title + ': ' + first + message.slice(1);
    }

    // загрузка доступна, когда чертёж выбран и прошёл проверку;
    // геоподоснова необязательна, но если выбрана — ждём и её проверки
    function allReady() {
        return fields.every(function (field) {
            return field.ready || (field.optional && !field.file());
        });
    }

    function refresh() {
        submitButton.disabled = !allReady();

        const messages = fields
            .filter(function (field) { return field.error; })
            .map(function (field) { return withTitle(field.title, field.error); });
        if (submitError) messages.push(submitError);
        errorLine.textContent = messages.join(' ');
    }

    function uploadField(root, optional) {
        const input = root.querySelector('input[type="file"]');
        const zone = root.querySelector('.drop');
        const prompt = zone.querySelector('.drop-prompt');
        const bar = zone.querySelector('.drop-bar');
        const name = zone.querySelector('.drop-name');
        const status = zone.querySelector('.drop-status');
        const percent = zone.querySelector('.drop-percent');
        const preview = previewOnUpload ? previewPanel(root) : NO_PREVIEW;

        const field = {
            name: input.name,
            title: root.querySelector('.field-label').textContent.trim(),
            optional: !!optional,
            ready: false,
            error: '',
            file: function () { return input.files[0]; },
            reset: reset,
        };

        let progress = null;
        let request = null;

        function stop() {
            if (progress) progress.stop();
            if (request) request.abort();
            progress = null;
            request = null;
        }

        function setPercent(fraction) {
            percent.textContent = Math.round(fraction * 100) + '%';
        }

        function reset() {
            stop();
            bar.hidden = true;
            prompt.hidden = false;
            status.hidden = false;
            setPercent(0);
            name.textContent = '';
            field.ready = false;
            field.error = '';
            preview.reset();
        }

        function setFile(file) {
            if (!file) return;

            const transfer = new DataTransfer();
            transfer.items.add(file);
            input.files = transfer.files;

            field.ready = false;
            field.error = '';
            submitError = '';
            refresh();

            prompt.hidden = true;
            bar.hidden = false;
            status.hidden = !previewOnUpload;
            name.textContent = file.name;
            preview.reset();

            stop();
            const tracker = previewOnUpload ? (progress = progressTracker(file, setPercent)) : NO_PROGRESS;

            checkDxf(file).then(function (problem) {
                if (problem) throw new Error(problem);
                if (input.files[0] !== file) return null;
                if (!previewOnUpload) return null;
                // превью проверяет любой DXF, поэтому файл уходит под общим именем file
                request = post(previewUrl, { file: file }, tracker.upload, true);
                return request;
            }).then(function (data) {
                tracker.stop();
                if (input.files[0] !== file) return;
                request = null;

                // без превью проверять нечего: файл выбран и прошёл локальную проверку
                if (!previewOnUpload) {
                    field.ready = true;
                    refresh();
                    return;
                }

                setPercent(1);
                // даём увидеть 100%, затем прячем статус
                setTimeout(function () {
                    if (input.files[0] !== file) return;
                    status.hidden = true;
                    preview.load(data);
                    field.ready = true;
                    refresh();
                }, 250);
            }).catch(function (err) {
                tracker.stop();
                if (input.files[0] !== file) return;
                request = null;
                status.hidden = true;
                field.error = err.message;
                // иначе повторный выбор того же файла (например, исправленного) не вызовет change
                input.value = '';
                refresh();
            });
        }

        input.addEventListener('change', function () { setFile(input.files[0]); });

        ['dragenter', 'dragover'].forEach(function (type) {
            zone.addEventListener(type, function (e) {
                e.preventDefault();
                zone.classList.add('is-over');
            });
        });

        ['dragleave', 'drop'].forEach(function (type) {
            zone.addEventListener(type, function (e) {
                e.preventDefault();
                zone.classList.remove('is-over');
            });
        });

        zone.addEventListener('drop', function (e) { setFile(e.dataTransfer.files[0]); });

        return field;
    }

    const planField = uploadField(panels.upload.querySelector('[data-field="file"]'));
    const geobaseField = uploadField(panels.upload.querySelector('[data-field="geobase"]'), true);
    const fields = [planField, geobaseField];

    function jobUrl(id, rest) {
        return jobsUrl.replace('JOB', id) + (rest || '');
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // этапы обработки: пройденные отмечаются, текущий подсвечивается, у подбора растений — счётчик структур
    function showStage(state) {
        const order = Array.prototype.map.call(stageItems, function (item) { return item.dataset.stage; });
        const index = order.indexOf(state.stage);
        stageItems.forEach(function (item, i) {
            item.classList.toggle('is-done', index > i || state.state === 'done');
            item.classList.toggle('is-active', i === index && state.state !== 'done');
        });
        if (state.state === 'queued') {
            stageLine.textContent = 'В очереди: сервер считает другой чертёж';
        } else if (state.stage === 'llm' && state.total) {
            stageLine.textContent = 'Подбор растений: ' + state.done + ' из ' + state.total + ' структур';
        } else {
            stageLine.textContent = state.stage_title || 'Запуск';
        }
    }

    async function waitFor(id) {
        for (;;) {
            const response = await fetch(jobUrl(id), { cache: 'no-store' });
            if (!response.ok) throw new Error('Задача обработки потерялась — загрузите файл ещё раз.');
            const state = await response.json();
            showStage(state);
            if (state.state === 'done') return state;
            if (state.state === 'error') throw new Error('Обработка не удалась: ' + (state.error || 'неизвестная ошибка'));
            if (state.state === 'cancelled') throw new Error('Обработка остановлена: ' + (state.error || 'отменена'));
            await sleep(POLL_MS);
        }
    }

    function showSummary(summary) {
        const plants = summary.plants || {};
        const placed = function (key) { return plants[key] ? Math.round(plants[key].placed) : 0; };
        const rows = [
            ['Структур', summary.structures],
            ['Зон с объяснением', summary.zones_explained + ' из ' + summary.zones],
            ['Новых деревьев', placed('tree')],
            ['Новых кустарников', placed('shrub')],
            ['Цветники и газон', placed('herbaceous_m2') + ' м²'],
            ['Нарушений правил', summary.violations],
        ];
        if (summary.by_rules) rows.push(['Подобрано правилами (без LLM)', summary.by_rules + ' из ' + summary.structures]);
        summaryList.innerHTML = rows.map(function (row) {
            return '<dt>' + row[0] + '</dt><dd>' + row[1] + '</dd>';
        }).join('');
    }

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        if (!allReady()) return;

        const files = {};
        fields.forEach(function (field) {
            if (field.ready) files[field.name] = field.file();
        });

        submitError = '';
        refresh();
        submitButton.disabled = true;
        stageLine.textContent = 'Отправка файла';
        stageItems.forEach(function (item) { item.classList.remove('is-done', 'is-active'); });
        goTo('processing');

        // страницу закрывают, пока чертёж считается, — задача отменяется и не держит очередь для других
        let running = null;
        function abandon(event) {
            if (!running || event.persisted) return;
            const body = new FormData();
            body.append('csrfmiddlewaretoken', form.elements.csrfmiddlewaretoken.value);
            navigator.sendBeacon(jobUrl(running, 'cancel/'), body);
        }
        window.addEventListener('pagehide', abandon);

        try {
            const started = await post(form.action, files);
            running = started.job;
            const state = await waitFor(started.job).finally(function () {
                running = null;
                window.removeEventListener('pagehide', abandon);
            });

            job = { id: started.job, name: started.name, summary: state.summary, view: null };
            downloadLink.href = jobUrl(job.id, 'dxf/');
            planLink.href = jobUrl(job.id, 'plan/');
            resultName.textContent = started.name;
            resultMeta.textContent = formatSize(started.size);
            showSummary(state.summary);
            goTo('result');
        } catch (err) {
            goTo('upload');
            submitError = err.message;
            refresh();
        }
    });

    viewerButton.addEventListener('click', async function () {
        if (!job) return;
        const opened = job;
        viewerButton.disabled = true;
        viewerButton.textContent = 'Загрузка просмотра…';
        try {
            if (!opened.view) {
                const response = await fetch(jobUrl(opened.id, 'view/'));
                if (!response.ok) throw new Error('Не удалось загрузить просмотр.');
                opened.view = await response.json();
            }
            if (job !== opened) return;
            window.GreeningViewer.open(opened.view, {
                name: opened.name,
                summary: opened.summary,
                links: { dxf: jobUrl(opened.id, 'dxf/'), plan: jobUrl(opened.id, 'plan/') },
            });
        } catch (err) {
            submitError = err.message;
        } finally {
            viewerButton.disabled = false;
            viewerButton.textContent = 'Открыть просмотр';
        }
    });

    restartButton.addEventListener('click', function () {
        job = null;
        form.reset();
        fields.forEach(function (field) { field.reset(); });
        submitError = '';
        refresh();
        goTo('upload');
    });
}());

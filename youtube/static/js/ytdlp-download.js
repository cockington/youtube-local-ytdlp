(function() {
    'use strict';
    var form = document.getElementById('ytdlp-form');
    if (!form || !window.fetch || !window.FormData) return;
    var statusBox = document.getElementById('ytdlp-status');
    var pollTimer = null;

    function formatSize(n) {
        if (!n) return '';
        var units = ['B', 'KiB', 'MiB', 'GiB'];
        var i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
    }

    function formatEta(s) {
        if (s == null) return '';
        s = Math.round(s);
        var m = Math.floor(s / 60);
        return m + ':' + ('0' + (s % 60)).slice(-2);
    }

    function el(tag, text, cls) {
        var e = document.createElement(tag);
        if (text != null) e.textContent = text;
        if (cls) e.className = cls;
        return e;
    }

    function render(job) {
        statusBox.textContent = '';
        var label = job.kind === 'm4a' ? 'Audio (m4a)' : 'Video';
        var line = el('div');
        if (job.status === 'queued') {
            line.textContent = label + ': queued...';
        } else if (job.status === 'downloading') {
            var txt = label + ': ' + job.percent + '%';
            if (job.part > 1) txt += ' (part ' + job.part + ')';
            if (job.total_bytes) txt += ' of ' + formatSize(job.total_bytes);
            if (job.speed) txt += ' - ' + formatSize(job.speed) + '/s';
            if (job.eta != null) txt += ' - ' + formatEta(job.eta) + ' left';
            line.textContent = txt;
        } else if (job.status === 'processing') {
            line.textContent = label + ': processing (merging/converting)...';
        } else if (job.status === 'finished') {
            line.textContent = 'Saved: ' + (job.filepath || job.filename) + ' ';
            var a = el('a', 'Save to computer');
            a.href = '/youtube.com/ytl-api/downloads/' + job.id + '/file';
            line.appendChild(a);
        } else if (job.status === 'cancelled') {
            line.textContent = label + ': cancelled';
        } else if (job.status === 'error') {
            line.appendChild(el('span', 'Download failed: ' + (job.error || 'unknown error'), 'ytdlp-error'));
        }
        statusBox.appendChild(line);

        if (job.status === 'queued' || job.status === 'downloading' || job.status === 'processing') {
            var bar = el('progress');
            bar.max = 100;
            if (job.status === 'downloading') bar.value = job.percent;
            statusBox.appendChild(bar);
        }
        if (job.warning) statusBox.appendChild(el('div', job.warning, 'ytdlp-error'));
    }

    function poll(jobId) {
        clearTimeout(pollTimer);
        fetch('/youtube.com/ytl-api/downloads/' + jobId, {headers: {'Accept': 'application/json'}})
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(job) {
                render(job);
                if (job.status === 'queued' || job.status === 'downloading' || job.status === 'processing') {
                    pollTimer = setTimeout(function() { poll(jobId); }, 1000);
                }
            })
            .catch(function(err) {
                statusBox.textContent = 'Lost track of download: ' + err.message;
            });
    }

    form.addEventListener('submit', function(e) {
        e.preventDefault();
        var data = new FormData(form);
        var submitter = e.submitter || document.activeElement;
        data.set('kind', submitter && submitter.value === 'm4a' ? 'm4a' : 'video');
        data.set('format', 'json');
        statusBox.textContent = 'Starting...';
        fetch(form.action, {method: 'POST', body: data, headers: {'Accept': 'application/json'}})
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(job) {
                render(job);
                poll(job.id);
            })
            .catch(function(err) {
                statusBox.textContent = '';
                statusBox.appendChild(el('span', 'Could not start download: ' + err.message, 'ytdlp-error'));
            });
    });
})();

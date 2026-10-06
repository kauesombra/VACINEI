(function () {
  "use strict";

  // Confirmação antes de ações destrutivas: <form data-confirm="texto">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Botão de imprimir: <button data-print>
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-print]")) window.print();
  });

  // ---------- envio de arquivos ----------
  var upload = document.getElementById("upload-form");
  if (upload) {
    var input = upload.querySelector("input[type=file]");
    var zone = upload.querySelector(".dropzone");
    var list = document.getElementById("file-list");
    var submit = upload.querySelector("button[type=submit]");

    function showFiles() {
      list.innerHTML = "";
      Array.prototype.forEach.call(input.files, function (f) {
        var li = document.createElement("li");
        li.textContent = f.name + " (" + (f.size / 1024 / 1024).toFixed(1).replace(".", ",") + " MB)";
        list.appendChild(li);
      });
      submit.disabled = input.files.length === 0;
    }
    input.addEventListener("change", showFiles);
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("over"); });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        showFiles();
      }
    });
    upload.addEventListener("submit", function () {
      // o OCR leva alguns segundos: mostra que está trabalhando e evita envio duplo
      upload.classList.add("busy");
      window.setTimeout(function () { submit.disabled = true; }, 0);
    });
    showFiles();
  }

  // ---------- revisão ----------
  var rows = document.getElementById("rows");
  if (rows) {
    var tpl = document.getElementById("row-template");
    var counter = parseInt(rows.getAttribute("data-next-rid"), 10) || 0;

    function syncCustom(row) {
      var sel = row.querySelector("select[name^=vaccine_]");
      var custom = row.querySelector(".custom");
      if (sel && custom) custom.hidden = sel.value !== "custom";
    }
    Array.prototype.forEach.call(rows.querySelectorAll(".row"), syncCustom);

    rows.addEventListener("change", function (e) {
      if (e.target.matches("select[name^=vaccine_]")) syncCustom(e.target.closest(".row"));
    });
    rows.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-remove-row]");
      if (btn) {
        btn.closest(".row").remove();
      }
    });
    var add = document.getElementById("add-row");
    if (add) {
      add.addEventListener("click", function () {
        var html = tpl.innerHTML.replace(/__RID__/g, String(counter++));
        var holder = document.createElement("div");
        holder.innerHTML = html.trim();
        var row = holder.firstElementChild;
        rows.appendChild(row);
        syncCustom(row);
        var first = row.querySelector("select");
        if (first) first.focus();
      });
    }
  }

  // ---------- filtro do histórico ----------
  var filter = document.getElementById("filter");
  if (filter) {
    var norm = function (s) {
      return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
    };
    filter.addEventListener("input", function () {
      var q = norm(filter.value.trim());
      document.querySelectorAll("[data-search]").forEach(function (el) {
        el.hidden = q !== "" && norm(el.getAttribute("data-search")).indexOf(q) === -1;
      });
    });
  }
})();

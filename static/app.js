(function () {
  var table = document.getElementById("line-items-table");
  var addButton = document.getElementById("add-line-item");
  if (!table || !addButton) return;

  var tbody = table.querySelector("tbody");
  var template = tbody.querySelector(".line-item-template");

  function wireRemove(row) {
    var btn = row.querySelector(".remove-line-item");
    if (!btn) return;
    btn.addEventListener("click", function () {
      row.remove();
    });
  }

  tbody.querySelectorAll(".line-item-row").forEach(wireRemove);

  addButton.addEventListener("click", function () {
    var clone = template.cloneNode(true);
    clone.classList.remove("line-item-template");
    clone.hidden = false;
    wireRemove(clone);
    tbody.insertBefore(clone, template);
  });
})();

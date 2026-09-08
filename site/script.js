// CC Dispatch site — copy-to-clipboard for code blocks. Vanilla, no deps.
(function () {
  document.querySelectorAll('.code-block').forEach(function (block) {
    var pre = block.querySelector('pre');
    var btn = block.querySelector('.copy-btn');
    if (!pre || !btn) return;
    btn.addEventListener('click', function () {
      var text = pre.textContent;
      var done = function () {
        var original = btn.textContent;
        btn.textContent = 'copied';
        btn.classList.add('copied');
        setTimeout(function () {
          btn.textContent = original;
          btn.classList.remove('copied');
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e) {}
        document.body.removeChild(ta);
        done();
      }
    });
  });
})();

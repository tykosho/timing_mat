Font Awesome 6 (all.min.css) and its webfonts, bundled for offline use (#1).

all.min.css normally lives in css/ and loads fonts from ../webfonts/. Here it sits next to
webfonts/, so its url(../webfonts/...) references were rewritten to url(webfonts/...).
Re-apply that if you replace the file: sed -i 's|url(../webfonts/|url(webfonts/|g' all.min.css

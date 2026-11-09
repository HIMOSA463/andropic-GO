package com.andropic.go;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.HttpURLConnection;
import java.net.NetworkInterface;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * andropic GO: оболочка с WebView. Весь интерфейс лежит в assets/index.html,
 * а сетевые запросы к ПК и Arduino выполняет нативный мост (без CORS-проблем).
 */
public class MainActivity extends Activity {

    private WebView web;
    private final ExecutorService httpPool = Executors.newFixedThreadPool(8);
    private final ExecutorService scanPool = Executors.newFixedThreadPool(96);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.parseColor("#0B0616"));
        getWindow().setNavigationBarColor(Color.parseColor("#0B0616"));

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0B0616"));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);

        web.setWebViewClient(new WebViewClient());
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.loadUrl("file:///android_asset/index.html");
    }

    @Override
    public void onBackPressed() {
        // даём интерфейсу шанс закрыть вложенный экран
        web.evaluateJavascript("(window.__back && window.__back()) === true", value -> {
            if (!"true".equals(value)) {
                finish();
            }
        });
    }

    @Override
    protected void onDestroy() {
        httpPool.shutdownNow();
        scanPool.shutdownNow();
        super.onDestroy();
    }

    /** Отправляет результат обратно в JS: window.__onNative(id, status, body) */
    private void reply(String id, int status, String body) {
        final String js = "window.__onNative(" + JSONObject.quote(id) + "," + status + ","
                + JSONObject.quote(body == null ? "" : body) + ")";
        web.post(() -> web.evaluateJavascript(js, null));
    }

    public class Bridge {

        /** Универсальный HTTP-запрос (для ПК и Arduino). */
        @JavascriptInterface
        public void http(final String id, final String method, final String url,
                         final String headersJson, final String body) {
            httpPool.execute(() -> {
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL(url).openConnection();
                    c.setConnectTimeout(3500);
                    c.setReadTimeout(6000);
                    c.setRequestMethod(method);
                    c.setUseCaches(false);
                    if (headersJson != null && headersJson.length() > 1) {
                        JSONObject h = new JSONObject(headersJson);
                        Iterator<String> it = h.keys();
                        while (it.hasNext()) {
                            String k = it.next();
                            c.setRequestProperty(k, h.getString(k));
                        }
                    }
                    if (body != null && body.length() > 0 && !"GET".equals(method)) {
                        c.setDoOutput(true);
                        byte[] data = body.getBytes(StandardCharsets.UTF_8);
                        c.setRequestProperty("Content-Type", "application/json");
                        c.setFixedLengthStreamingMode(data.length);
                        try (OutputStream os = c.getOutputStream()) {
                            os.write(data);
                        }
                    }
                    int code = c.getResponseCode();
                    InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
                    reply(id, code, readAll(is));
                } catch (Exception e) {
                    reply(id, 0, String.valueOf(e.getMessage()));
                } finally {
                    if (c != null) c.disconnect();
                }
            });
        }

        /** Локальный IPv4-адрес телефона в Wi-Fi сети. */
        @JavascriptInterface
        public String localIp() {
            String ip = findLocalIp();
            return ip == null ? "" : ip;
        }

        /** Поиск устройств andropic в подсети /24 (ПК-сервер :8765 и ESP :80). */
        @JavascriptInterface
        public void scan(final String id) {
            new Thread(() -> {
                String ip = findLocalIp();
                if (ip == null) {
                    reply(id, 200, "[]");
                    return;
                }
                final String prefix = ip.substring(0, ip.lastIndexOf('.') + 1);
                final int[] ports = {8765, 80};
                final List<JSONObject> found = Collections.synchronizedList(new ArrayList<>());
                final CountDownLatch latch = new CountDownLatch(254 * ports.length);

                for (int i = 1; i <= 254; i++) {
                    final String host = prefix + i;
                    for (final int port : ports) {
                        scanPool.execute(() -> {
                            try {
                                String r = probe(host, port);
                                if (r != null && r.contains("andropic")) {
                                    JSONObject o = new JSONObject(r);
                                    o.put("ip", host);
                                    o.put("port", port);
                                    found.add(o);
                                }
                            } catch (Exception ignored) {
                            } finally {
                                latch.countDown();
                            }
                        });
                    }
                }
                try {
                    latch.await();
                } catch (InterruptedException ignored) {
                }
                JSONArray arr = new JSONArray();
                for (JSONObject o : found) arr.put(o);
                reply(id, 200, arr.toString());
            }).start();
        }
    }

    private static String probe(String host, int port) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + host + ":" + port + "/api/ping").openConnection();
            c.setConnectTimeout(400);
            c.setReadTimeout(900);
            if (c.getResponseCode() != 200) return null;
            return readAll(c.getInputStream());
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String findLocalIp() {
        try {
            String fallback = null;
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        if (ni.getName().startsWith("wlan")) return a.getHostAddress();
                        if (fallback == null) fallback = a.getHostAddress();
                    }
                }
            }
            return fallback;
        } catch (Exception e) {
            return null;
        }
    }

    private static String readAll(InputStream is) throws Exception {
        if (is == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        is.close();
        return bos.toString("UTF-8");
    }
}

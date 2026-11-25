import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

// plasma-ai-quota — AI coding subscription quotas in your Plasma panel.
// Data comes from contents/code/ai_quota.py (cached, tokens never leave the collector).
PlasmoidItem {
    id: root

    readonly property string collector: Qt.resolvedUrl("../code/ai_quota.py").toString().replace("file://", "")
    property var data: ({ services: [] })
    property bool busy: false

    // ───────── translations (English default, Turkish when the system locale is tr) ─────────
    readonly property string lang: Qt.locale().name.substring(0, 2) === "tr" ? "tr" : "en"
    readonly property var strings: ({
        en: {
            title: "AI Quota", updated: "Updated %1", refresh: "Refresh now", open: "Open usage page",
            resets: "resets %1", w_5h: "5-hour", w_day: "Daily", w_week: "Weekly", w_month: "Monthly",
            w_extra: "Extra usage", w_credit: "Monthly credit", w_custom: "Window",
            credits_left: "Credits left", subscription_left: "Subscription credit left", period_requests: "This period",
            e_not_logged_in: "Not signed in (%1)", e_token_expired: "Session expired — open %1 once",
            e_auth: "Access denied (%1)", e_rate_limited: "Rate limited — backing off", e_http: "Server error (%1)",
            e_network: "Network error (%1)", e_no_windows: "No usage data in response", e_limit_reached: "Limit reached",
            e_stale_local: "Live query failed; last local record %1", e_needs_config: "Set %1 in ~/.config/plasma-ai-quota/config.json",
            e_needs_dependency: "Install %1", e_api_changed: "Service API changed — update the widget",
            e_disabled: "Disabled", e_unexpected: "Error: %1", e_no_data: "No data", credits: "credits"
        },
        tr: {
            title: "AI Kota", updated: "Güncellendi %1", refresh: "Şimdi yenile", open: "Kullanım sayfasını aç",
            resets: "sıfırlanma %1", w_5h: "5 saatlik", w_day: "Günlük", w_week: "Haftalık", w_month: "Aylık",
            w_extra: "Ek kullanım", w_credit: "Aylık kredi", w_custom: "Pencere",
            credits_left: "Kalan kredi", subscription_left: "Abonelik kredisi kalan", period_requests: "Bu dönem",
            e_not_logged_in: "Giriş yok (%1)", e_token_expired: "Oturum süresi doldu — %1 bir kez aç",
            e_auth: "Yetki reddedildi (%1)", e_rate_limited: "Hız sınırı — bekleniyor", e_http: "Sunucu hatası (%1)",
            e_network: "Ağ hatası (%1)", e_no_windows: "Yanıtta kullanım verisi yok", e_limit_reached: "Limit doldu",
            e_stale_local: "Canlı sorgu başarısız; son yerel kayıt %1", e_needs_config: "~/.config/plasma-ai-quota/config.json içinde %1 ayarla",
            e_needs_dependency: "%1 kur", e_api_changed: "Servis API'si değişti — widget'ı güncelle",
            e_disabled: "Kapalı", e_unexpected: "Hata: %1", e_no_data: "Veri yok", credits: "kredi"
        }
    })
    function t(key, arg) {
        const s = (strings[lang][key] !== undefined ? strings[lang][key] : (strings.en[key] || key))
        return arg !== undefined ? s.replace("%1", arg) : s
    }
    function windowName(w) { return t("w_" + w.kind) + (w.scope ? " (" + w.scope + ")" : "") }
    function errorText(s) { return s.error ? t("e_" + s.error, s.error_detail || "") : "" }
    function fmtTime(ts) {
        if (!ts) return ""
        return Qt.formatDateTime(new Date(ts * 1000), "dd.MM HH:mm")
    }

    // ───────── colours (Gruvbox) ─────────
    readonly property color cGreen: "#b8bb26"
    readonly property color cYellow: "#fabd2f"
    readonly property color cRed: "#fb4934"
    readonly property color cGray: "#928374"
    function colorFor(p) { return (p === null || p === undefined) ? cGray : (p < 60 ? cGreen : (p < 85 ? cYellow : cRed)) }

    readonly property var logos: ({ claude: "claude.svg", codex: "openai.svg", opencode: "opencode.svg",
                                    commandcode: "commandcode.png", replit: "replit.svg" })
    function logo(id) { return logos[id] ? Qt.resolvedUrl("../icons/" + logos[id]) : "" }

    function worst(s) {
        let m = null
        for (const w of (s.windows || [])) if (w.percent !== null && (m === null || w.percent > m.percent)) m = w
        return m
    }
    readonly property var shown: (data.services || []).filter(s => s.state !== "disabled" && ((s.windows || []).length > 0 || !!s.badge))

    toolTipMainText: t("title")
    toolTipSubText: (data.services || []).filter(s => s.state !== "disabled").map(s => {
        const w = worst(s)
        return s.name + ": " + (s.badge ? s.badge + " " + t("credits") : (w ? Math.round(w.percent) + "% (" + windowName(w) + ")" : t("e_no_data")))
    }).join("\n") + (data.updated_at ? "\n" + t("updated", fmtTime(data.updated_at)) : "")

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, d) => {
            const out = (d["stdout"] || "").trim()
            try { if (out) root.data = JSON.parse(out) } catch (e) { console.warn("plasma-ai-quota: bad JSON", e) }
            root.busy = false
            disconnectSource(source)
        }
    }
    function refresh(force) {
        if (busy) return
        busy = true
        exec.connectSource("python3 '" + collector + "' --json" + (force ? " --force" : "") + " #" + Date.now())
    }
    Timer {
        interval: 5 * 60 * 1000
        running: true; repeat: true; triggeredOnStart: true
        onTriggered: root.refresh(false)
    }

    // ───────── panel ─────────
    compactRepresentation: MouseArea {
        Layout.minimumWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: Layout.minimumWidth
        hoverEnabled: true
        onClicked: root.expanded = !root.expanded
        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.smallSpacing * 2
            Repeater {
                model: root.shown
                delegate: RowLayout {
                    required property var modelData
                    readonly property var w: root.worst(modelData)
                    readonly property var pct: modelData.badge ? modelData.badge_percent : (w ? w.percent : null)
                    spacing: 3
                    Rectangle {
                        Layout.preferredWidth: 4
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        radius: 2; color: Qt.rgba(1, 1, 1, 0.15)
                        Rectangle {
                            anchors.bottom: parent.bottom; width: parent.width; radius: 2
                            height: parent.height * Math.min(1, Math.max(0.06, modelData.badge ? 1 - (pct || 0) / 100 : (pct || 0) / 100))
                            color: root.colorFor(pct)
                        }
                    }
                    Image {
                        source: root.logo(modelData.id)
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        sourceSize.width: 64; sourceSize.height: 64
                        fillMode: Image.PreserveAspectFit; smooth: true
                    }
                    PlasmaComponents.Label {
                        text: modelData.badge ? modelData.badge : (w ? Math.round(w.percent) + "%" : "–")
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.family: "monospace"
                        color: modelData.state === "error" ? root.cRed : Kirigami.Theme.textColor
                    }
                }
            }
            PlasmaComponents.Label {
                visible: root.shown.length === 0
                text: "AI"
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }
        }
    }

    // ───────── popup ─────────
    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * 24
        Layout.preferredHeight: Math.min(list.implicitHeight + header.implicitHeight + Kirigami.Units.gridUnit * 2, Kirigami.Units.gridUnit * 34)
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            id: header
            Layout.fillWidth: true
            Kirigami.Heading { level: 4; text: root.t("title"); Layout.fillWidth: true }
            PlasmaComponents.Label {
                text: root.data.updated_at ? root.t("updated", root.fmtTime(root.data.updated_at)) : ""
                opacity: 0.7; font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }
            PlasmaComponents.ToolButton {
                icon.name: "view-refresh"
                enabled: !root.busy
                onClicked: root.refresh(true)
                QQC2.ToolTip.text: root.t("refresh"); QQC2.ToolTip.visible: hovered
            }
        }

        QQC2.ScrollView {
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth
            ColumnLayout {
                id: list
                width: parent.width
                spacing: Kirigami.Units.largeSpacing
                Repeater {
                    model: (root.data.services || []).filter(s => s.state !== "disabled")
                    delegate: ColumnLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 3
                        RowLayout {
                            Layout.fillWidth: true
                            Image {
                                source: root.logo(modelData.id)
                                Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                                Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
                                sourceSize.width: 64; sourceSize.height: 64
                                fillMode: Image.PreserveAspectFit; smooth: true
                            }
                            PlasmaComponents.Label {
                                text: "<b>" + modelData.name + "</b>" + (modelData.plan ? "  <span style='color:" + root.cGray + "'>" + modelData.plan + "</span>" : "")
                                textFormat: Text.StyledText; Layout.fillWidth: true; elide: Text.ElideRight
                            }
                            PlasmaComponents.ToolButton {
                                visible: !!modelData.url
                                icon.name: "internet-web-browser"
                                display: QQC2.AbstractButton.IconOnly
                                onClicked: Qt.openUrlExternally(modelData.url)
                                QQC2.ToolTip.text: root.t("open"); QQC2.ToolTip.visible: hovered
                            }
                        }
                        Repeater {
                            model: modelData.windows || []
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 1
                                RowLayout {
                                    Layout.fillWidth: true
                                    PlasmaComponents.Label { text: root.windowName(modelData); Layout.fillWidth: true; font.pixelSize: Kirigami.Theme.smallFont.pixelSize }
                                    PlasmaComponents.Label {
                                        text: (modelData.percent !== null ? Math.round(modelData.percent) + "%" : "–")
                                              + (modelData.detail ? "  · " + modelData.detail : "")
                                              + (modelData.resets_at ? "  · " + root.t("resets", root.fmtTime(modelData.resets_at)) : "")
                                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize; opacity: 0.85
                                    }
                                }
                                Rectangle {
                                    Layout.fillWidth: true; Layout.preferredHeight: 6; radius: 3
                                    color: Qt.rgba(1, 1, 1, 0.12)
                                    Rectangle {
                                        height: parent.height; radius: 3
                                        width: parent.width * Math.min(1, Math.max(0, (modelData.percent || 0) / 100))
                                        color: root.colorFor(modelData.percent)
                                    }
                                }
                            }
                        }
                        Repeater {
                            model: modelData.info || []
                            delegate: PlasmaComponents.Label {
                                required property var modelData
                                text: root.t(modelData.key) + ": " + modelData.value
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize; opacity: 0.75
                                Layout.fillWidth: true; wrapMode: Text.Wrap
                            }
                        }
                        PlasmaComponents.Label {
                            visible: !!modelData.error
                            text: root.errorText(modelData)
                            color: modelData.state === "error" ? root.cRed : root.cYellow
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            Layout.fillWidth: true; wrapMode: Text.Wrap
                        }
                        Kirigami.Separator { Layout.fillWidth: true; Layout.topMargin: Kirigami.Units.smallSpacing }
                    }
                }
            }
        }
    }
}

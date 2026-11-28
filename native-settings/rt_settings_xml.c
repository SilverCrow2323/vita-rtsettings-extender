#include "rt_settings_xml.h"
#include <string.h>

static const char ROOT_PLUGIN[] = "plugin=\"system_settings_plugin\"";
static const char ROOT_ID[] = "id=\"system_settings\"";
static const char RT_ID[] = "id=\"rt_settings_extended\"";
static const char ROOT_CLOSE[] = "</system_settings>";
static const char LIST_CLOSE[] = "</setting_list>";

/* One real, navigable Settings entry. The only leaf is a read-only status
 * action; no plugin controls are advertised before their backends exist. */
static const char RT_ENTRY[] =
    "<setting_list id=\"rt_settings_extended\" title=\"Rt:Settings Extended\" icon=\"tex_spanner\">"
      "<setting_list id=\"rt_preferences_settings_management\" title=\"Preferences &amp; Settings Management\" icon=\"tex_spanner\">"
        "<setting_list id=\"rt_manager\" title=\"Rt Manager\" icon=\"tex_spanner\">"
          "<button id=\"rt_manager_about\" title=\"Rt Manager Status\" icon=\"tex_spanner\" hint=\"rt_manager_status_hint\" success_dialog_message=\"rt_manager_installed\"/>"
          "<button id=\"rt_plugin_audit\" title=\"Plugin Configuration Audit\" icon=\"tex_spanner\" hint=\"rt_plugin_audit_hint\" success_dialog_message=\"rt_plugin_audit_result\"/>"
        "</setting_list>"
      "</setting_list>"
    "</setting_list>";

static const char *find_bytes(const char *haystack, size_t haystack_length,
                              const char *needle, size_t needle_length) {
    size_t i;
    if (!haystack || !needle || !needle_length || needle_length > haystack_length) return NULL;
    for (i = 0; i <= haystack_length - needle_length; ++i)
        if (memcmp(haystack + i, needle, needle_length) == 0) return haystack + i;
    return NULL;
}

static const char *find_last_bytes(const char *haystack, size_t haystack_length,
                                   const char *needle, size_t needle_length) {
    const char *found = NULL, *cursor = haystack;
    size_t remaining = haystack_length;
    while (remaining >= needle_length) {
        const char *next = find_bytes(cursor, remaining, needle, needle_length);
        if (!next) break;
        found = next;
        remaining -= (size_t)(next - cursor) + needle_length;
        cursor = next + needle_length;
    }
    return found;
}

rt_settings_xml_result rt_settings_xml_splice_root(
    const char *source, size_t source_length,
    char *destination, size_t destination_capacity,
    size_t *out_length) {
    const char *root_plugin, *root_id, *root_close, *insert_at;
    size_t entry_length, prefix_length, suffix_length, result_length;
    if (!source || !destination || !out_length || !source_length ||
        source_length >= destination_capacity) return RT_SETTINGS_XML_ERROR;
    *out_length = 0;
    root_plugin = find_bytes(source, source_length, ROOT_PLUGIN, sizeof(ROOT_PLUGIN) - 1);
    root_id = find_bytes(source, source_length, ROOT_ID, sizeof(ROOT_ID) - 1);
    root_close = find_bytes(source, source_length, ROOT_CLOSE, sizeof(ROOT_CLOSE) - 1);
    if (!root_plugin || !root_id) return RT_SETTINGS_XML_UNCHANGED;
    if (!root_close) return RT_SETTINGS_XML_ERROR;
    if (find_bytes(source, source_length, RT_ID, sizeof(RT_ID) - 1))
        return RT_SETTINGS_XML_UNCHANGED;
    insert_at = find_last_bytes(source, source_length, LIST_CLOSE, sizeof(LIST_CLOSE) - 1);
    if (!insert_at || insert_at > root_close) return RT_SETTINGS_XML_ERROR;
    prefix_length = (size_t)(insert_at - source);
    suffix_length = source_length - prefix_length;
    entry_length = sizeof(RT_ENTRY) - 1;
    if (prefix_length >= destination_capacity ||
        entry_length >= destination_capacity - prefix_length ||
        suffix_length > destination_capacity - prefix_length - entry_length - 1)
        return RT_SETTINGS_XML_ERROR;
    result_length = prefix_length + entry_length + suffix_length;
    memcpy(destination, source, prefix_length);
    memcpy(destination + prefix_length, RT_ENTRY, entry_length);
    memcpy(destination + prefix_length + entry_length, insert_at, suffix_length);
    destination[result_length] = '\0';
    *out_length = result_length;
    return RT_SETTINGS_XML_INSERTED;
}

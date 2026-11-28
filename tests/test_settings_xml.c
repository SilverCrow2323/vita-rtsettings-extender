#include "../native-settings/rt_settings_xml.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static const char root_xml[] =
    "<?xml version=\"1.0\"?>"
    "<system_settings version=\"1.0\" plugin=\"system_settings_plugin\">"
    "<setting_list id=\"system_settings\" title=\"msg_settings\">"
    "<link id=\"network_settings\" title=\"Network\"/>"
    "</setting_list></system_settings>";

int main(void) {
    char output[RT_SETTINGS_XML_BUFFER_SIZE];
    size_t length = 0;
    rt_settings_xml_result result;

    result = rt_settings_xml_splice_root(root_xml, sizeof(root_xml)-1,
                                         output, sizeof(output), &length);
    assert(result == RT_SETTINGS_XML_INSERTED);
    assert(length > sizeof(root_xml)-1);
    assert(strstr(output, "id=\"rt_settings_extended\"") != NULL);
    assert(strstr(output, "rt_manager_about") != NULL);
    assert(strstr(output, "rt_plugin_audit") != NULL);
    assert(strstr(output, "Plugin Configuration Audit") != NULL);
    assert(strstr(output, "id=\"network_settings\"") != NULL);
    assert(strstr(output, "</system_settings>") != NULL);
    assert(output[length] == '\0');

    /* Idempotence: a second pass leaves the already-modified XML untouched. */
    {
        char second[RT_SETTINGS_XML_BUFFER_SIZE];
        size_t second_length = 0;
        result = rt_settings_xml_splice_root(output, length, second,
                                             sizeof(second), &second_length);
        assert(result == RT_SETTINGS_XML_UNCHANGED);
        assert(second_length == 0);
    }

    /* A non-root plugin page must be passed through unchanged. */
    {
        static const char other[] =
            "<system_settings plugin=\"network_settings_plugin\">"
            "<setting_list id=\"network\"></setting_list></system_settings>";
        result = rt_settings_xml_splice_root(other, sizeof(other)-1, output,
                                             sizeof(output), &length);
        assert(result == RT_SETTINGS_XML_UNCHANGED);
    }

    /* A malformed root with no list terminator is rejected, never rewritten. */
    {
        static const char malformed[] =
            "<system_settings plugin=\"system_settings_plugin\" "
            "id=\"system_settings\">";
        result = rt_settings_xml_splice_root(malformed, sizeof(malformed)-1,
                                             output, sizeof(output), &length);
        assert(result == RT_SETTINGS_XML_ERROR);
    }

    /* Capacity failure is fail-closed. */
    result = rt_settings_xml_splice_root(root_xml, sizeof(root_xml)-1,
                                         output, 8, &length);
    assert(result == RT_SETTINGS_XML_ERROR);

    /* Null and empty arguments are rejected. */
    assert(rt_settings_xml_splice_root(NULL, 1, output, sizeof(output), &length)
           == RT_SETTINGS_XML_ERROR);
    assert(rt_settings_xml_splice_root(root_xml, 0, output, sizeof(output), &length)
           == RT_SETTINGS_XML_ERROR);

    puts("Native Settings XML splice tests passed.");
    return 0;
}

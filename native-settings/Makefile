VITASDK ?= $(shell echo $$VITASDK)
CC := arm-vita-eabi-gcc
ELF_CREATE := vita-elf-create
CFLAGS := -std=c99 -O2 -Wall -Wextra -Wpedantic -fno-strict-aliasing
CPPFLAGS := -I$(VITASDK)/arm-vita-eabi/include -I. -I../include
LDFLAGS := -Wl,-q -nostartfiles
TARGET := rtsettings
OBJECTS := rt_settings_main.o rt_settings_xml.o rt_settings_runtime.o plugin_inventory.o plugin_compatibility.o

.PHONY: all clean
all: $(TARGET).suprx

rt_settings_main.o: rt_settings_main.c rt_settings_xml.h rt_settings_runtime.h ../include/plugin_inventory.h ../include/plugin_compatibility.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

rt_settings_xml.o: rt_settings_xml.c rt_settings_xml.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

rt_settings_runtime.o: rt_settings_runtime.c rt_settings_runtime.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

plugin_inventory.o: ../source/plugin-inventory/plugin_inventory.c ../include/plugin_inventory.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

plugin_compatibility.o: ../source/plugin-compatibility/plugin_compatibility.c ../include/plugin_compatibility.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(TARGET).elf: $(OBJECTS)
	$(CC) $(LDFLAGS) -o $@ $^ -ltaihen_stub

$(TARGET).suprx: $(TARGET).elf rtsettings.yml
	$(ELF_CREATE) -e rtsettings.yml $< $@

clean:
	rm -f $(OBJECTS) $(TARGET).elf $(TARGET).suprx

#!/bin/bash
# Construit la couche Vulkan « flux » (libpilote_flux.so) et son test, et installe le manifeste UTILISATEUR
# (~/.local/share/vulkan/implicit_layer.d, rien dans le système). En-têtes : ./Vulkan-Headers (clone local).
set -e
cd "$(dirname "$0")"
INC=Vulkan-Headers/include
[ -d "$INC/vulkan" ] || { echo "en-têtes absents : git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git"; exit 1; }
gcc -O2 -Wall -Wextra -fPIC -fvisibility=hidden -shared -I"$INC" -o libpilote_flux.so pilote_flux.c -lpthread
gcc -O2 -Wall -I"$INC" -o test_instance test_instance.c -L/usr/lib/x86_64-linux-gnu -l:libvulkan.so.1
mkdir -p ~/.local/share/vulkan/implicit_layer.d
cat > ~/.local/share/vulkan/implicit_layer.d/VK_LAYER_PILOTE_flux.json <<EOF
{
  "file_format_version": "1.0.0",
  "layer": {
    "name": "VK_LAYER_PILOTE_flux",
    "type": "GLOBAL",
    "library_path": "$(pwd)/libpilote_flux.so",
    "api_version": "1.3.0",
    "implementation_version": "1",
    "description": "JARVIS pilote-ecran : le flux de la carte, image par image (lecture seule)",
    "enable_environment": {"PILOTE_FLUX": "1"},
    "disable_environment": {"PILOTE_FLUX_OFF": "1"}
  }
}
EOF
echo "couche construite : $(pwd)/libpilote_flux.so ; manifeste : ~/.local/share/vulkan/implicit_layer.d/VK_LAYER_PILOTE_flux.json"
echo "activation : PILOTE_FLUX=1 <programme Vulkan>  (ex. PILOTE_FLUX=1 steam)"

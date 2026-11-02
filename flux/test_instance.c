/* test_instance.c — vérifie que la couche VK_LAYER_PILOTE_flux s'intercale : instance, appareil, quelques
 * commandes enregistrées (jamais soumises), destruction. Lance avec PILOTE_FLUX=1. */
#include <vulkan/vulkan.h>
#include <stdio.h>

int main(void) {
    VkApplicationInfo ai = {VK_STRUCTURE_TYPE_APPLICATION_INFO, NULL, "test-pilote-flux", 1, "aucun", 0, VK_API_VERSION_1_3};
    VkInstanceCreateInfo ci = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, NULL, 0, &ai, 0, NULL, 0, NULL};
    VkInstance inst;
    if (vkCreateInstance(&ci, NULL, &inst) != VK_SUCCESS) { puts("instance : ÉCHEC"); return 1; }
    uint32_t n = 0;
    vkEnumeratePhysicalDevices(inst, &n, NULL);
    VkPhysicalDevice pds[8];
    if (n > 8) n = 8;
    vkEnumeratePhysicalDevices(inst, &n, pds);
    printf("instance OK, %u carte(s)\n", n);
    uint32_t choix = 0;
    for (uint32_t i = 0; i < n; i++) {
        VkPhysicalDeviceProperties p;
        vkGetPhysicalDeviceProperties(pds[i], &p);
        printf("  - %s%s\n", p.deviceName, p.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU ? " (carte dédiée)" : "");
        if (p.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) choix = i;
    }
    if (!n) return 1;
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qi = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, NULL, 0, 0, 1, &prio};
    VkDeviceCreateInfo di = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, NULL, 0, 1, &qi, 0, NULL, 0, NULL, NULL};
    VkDevice dev;
    VkResult r = vkCreateDevice(pds[choix], &di, NULL, &dev);
    printf("appareil : %s\n", r == VK_SUCCESS ? "OK" : "ÉCHEC");
    if (r != VK_SUCCESS) return 1;
    VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL, 0, 0};
    VkCommandPool pool;
    if (vkCreateCommandPool(dev, &pci, NULL, &pool) == VK_SUCCESS) {
        VkCommandBufferAllocateInfo cai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, pool, VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
        VkCommandBuffer cb;
        if (vkAllocateCommandBuffers(dev, &cai, &cb) == VK_SUCCESS) {
            VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL, 0, NULL};
            vkBeginCommandBuffer(cb, &bi);
            vkCmdDispatch(cb, 1, 1, 1);          /* trois commandes enregistrées, jamais soumises */
            vkCmdDispatch(cb, 2, 2, 2);
            vkCmdDispatch(cb, 3, 3, 3);
            vkEndCommandBuffer(cb);
            puts("3 commandes enregistrées (calculs)");
        }
        vkDestroyCommandPool(dev, pool, NULL);
    }
    vkDestroyDevice(dev, NULL);
    vkDestroyInstance(inst, NULL);
    puts("fin");
    return 0;
}

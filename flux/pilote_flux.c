/* pilote_flux.c — couche Vulkan « flux » de JARVIS (09/10/2026).
 *
 * Demande de Félix : « un programme qui s'intercale sur la carte graphique et qui, au lieu d'envoyer des données
 * graphiques, t'envoie des données, comme dans Matrix, image par image ». Cette couche se place entre le jeu et le
 * pilote NVIDIA (chargeur Vulkan, couche implicite) et écrit, à CHAQUE image présentée, une ligne JSON :
 * cadence, appels de dessin, sommets, instances, dessins indirects, calculs, pipelines liés, passes de rendu,
 * résolution ; plus une ligne à chaque création de pipelines (= compilation de shaders) et de swapchain.
 * Lecture seule : rien n'est modifié dans ce que reçoit la carte. Jeux Windows : via VKD3D-Proton / DXVK.
 *
 * Sortie : $PILOTE_FLUX_FICHIER, sinon /run/user/<uid>/pilote-ecran/flux.jsonl, sinon ~/.cache/pilote-ecran/flux.jsonl.
 * Activation : variable PILOTE_FLUX=1 (manifeste ~/.local/share/vulkan/implicit_layer.d/VK_LAYER_PILOTE_flux.json).
 * Compilation : voir construire.sh (gcc, en-têtes Vulkan locaux, aucune bibliothèque liée).
 */
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define NOM_COUCHE "VK_LAYER_PILOTE_flux"
#define MAX_OBJ 16
#define EXPORT __attribute__((visibility("default")))

static inline void *cle(const void *poignee) { return *(void **)poignee; }   /* clé de répartition du chargeur */

typedef struct {
    void *cle;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkDestroyInstance DestroyInstance;
    PFN_vkEnumerateDeviceExtensionProperties EnumerateDeviceExtensionProperties;
    PFN_vkGetPhysicalDeviceProperties GetPhysicalDeviceProperties;
    char appli[96];
} Instance;

typedef struct {
    void *cle;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkDestroyDevice DestroyDevice;
    PFN_vkQueuePresentKHR QueuePresentKHR;
    PFN_vkCmdDraw CmdDraw;
    PFN_vkCmdDrawIndexed CmdDrawIndexed;
    PFN_vkCmdDrawIndirect CmdDrawIndirect;
    PFN_vkCmdDrawIndexedIndirect CmdDrawIndexedIndirect;
    PFN_vkCmdDrawIndirectCount CmdDrawIndirectCount;
    PFN_vkCmdDrawIndexedIndirectCount CmdDrawIndexedIndirectCount;
    PFN_vkCmdDispatch CmdDispatch;
    PFN_vkCmdBindPipeline CmdBindPipeline;
    PFN_vkCmdBeginRenderPass CmdBeginRenderPass;
    PFN_vkCmdBeginRenderPass2 CmdBeginRenderPass2;
    PFN_vkCmdBeginRendering CmdBeginRendering;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
    PFN_vkCreateGraphicsPipelines CreateGraphicsPipelines;
    PFN_vkCreateComputePipelines CreateComputePipelines;
    /* compteurs de l'image en cours (remis à zéro à chaque présentation) */
    uint64_t dessins, sommets, instances, indexes, indirects, calculs, pipelines_lies, passes;
    /* totaux de la vie de l'appareil */
    uint64_t images, pipelines_crees, dessins_total;
    uint32_t largeur, hauteur;
    struct timespec derniere;
    char appli[96], carte[96];
    FILE *sortie;
} Device;

static Instance instances[MAX_OBJ];
static Device devices[MAX_OBJ];
static pthread_mutex_t verrou = PTHREAD_MUTEX_INITIALIZER;

static Instance *inst_de(void *k) {
    for (int i = 0; i < MAX_OBJ; i++) if (instances[i].cle == k) return &instances[i];
    return NULL;
}
static Device *dev_de(void *k) {
    for (int i = 0; i < MAX_OBJ; i++) if (devices[i].cle == k) return &devices[i];
    for (int i = 0; i < MAX_OBJ; i++) if (devices[i].cle) return &devices[i];   /* secours : ne jamais couper la chaîne */
    return NULL;
}

static double maintenant_ms(void) {
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static FILE *ouvrir_sortie(void) {
    char chemin[512];
    const char *env = getenv("PILOTE_FLUX_FICHIER");
    FILE *f = NULL;
    if (env && *env) f = fopen(env, "a");
    if (!f) {
        snprintf(chemin, sizeof chemin, "/run/user/%u/pilote-ecran/flux.jsonl", (unsigned)getuid());
        f = fopen(chemin, "a");
    }
    if (!f) {
        const char *home = getenv("HOME");
        if (home && *home) {
            snprintf(chemin, sizeof chemin, "%s/.cache/pilote-ecran", home);
            mkdir(chemin, 0700);
            snprintf(chemin, sizeof chemin, "%s/.cache/pilote-ecran/flux.jsonl", home);
            f = fopen(chemin, "a");
        }
    }
    return f;
}

static void echapper(const char *s, char *dst, size_t n) {   /* JSON minimal : guillemets et antislashs */
    size_t j = 0;
    for (size_t i = 0; s && s[i] && j + 2 < n; i++) {
        if (s[i] == '"' || s[i] == '\\') dst[j++] = '\\';
        dst[j++] = (unsigned char)s[i] < 32 ? ' ' : s[i];
    }
    dst[j] = 0;
}

/* ───────────── instance ───────────── */
static VKAPI_ATTR VkResult VKAPI_CALL pf_CreateInstance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *alloc, VkInstance *inst) {
    VkLayerInstanceCreateInfo *li = (VkLayerInstanceCreateInfo *)ci->pNext;
    while (li && !(li->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && li->function == VK_LAYER_LINK_INFO))
        li = (VkLayerInstanceCreateInfo *)li->pNext;
    if (!li) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = li->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    li->u.pLayerInfo = li->u.pLayerInfo->pNext;
    PFN_vkCreateInstance creer = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    if (!creer) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = creer(ci, alloc, inst);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&verrou);
    Instance *I = inst_de(NULL);
    if (I) {
        memset(I, 0, sizeof *I);
        I->cle = cle(*inst);
        I->gipa = gipa;
        I->DestroyInstance = (PFN_vkDestroyInstance)gipa(*inst, "vkDestroyInstance");
        I->EnumerateDeviceExtensionProperties = (PFN_vkEnumerateDeviceExtensionProperties)gipa(*inst, "vkEnumerateDeviceExtensionProperties");
        I->GetPhysicalDeviceProperties = (PFN_vkGetPhysicalDeviceProperties)gipa(*inst, "vkGetPhysicalDeviceProperties");
        snprintf(I->appli, sizeof I->appli, "%s", (ci->pApplicationInfo && ci->pApplicationInfo->pApplicationName) ? ci->pApplicationInfo->pApplicationName : "?");
    }
    pthread_mutex_unlock(&verrou);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL pf_DestroyInstance(VkInstance inst, const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&verrou);
    Instance *I = inst_de(cle(inst));
    PFN_vkDestroyInstance suivant = I ? I->DestroyInstance : NULL;
    if (I) I->cle = NULL;
    pthread_mutex_unlock(&verrou);
    if (suivant) suivant(inst, alloc);
}

/* ───────────── appareil ───────────── */
static VKAPI_ATTR VkResult VKAPI_CALL pf_CreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo *ci, const VkAllocationCallbacks *alloc, VkDevice *dev) {
    VkLayerDeviceCreateInfo *li = (VkLayerDeviceCreateInfo *)ci->pNext;
    while (li && !(li->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && li->function == VK_LAYER_LINK_INFO))
        li = (VkLayerDeviceCreateInfo *)li->pNext;
    if (!li) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = li->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = li->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    li->u.pLayerInfo = li->u.pLayerInfo->pNext;
    PFN_vkCreateDevice creer = (PFN_vkCreateDevice)gipa(VK_NULL_HANDLE, "vkCreateDevice");
    if (!creer) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = creer(pd, ci, alloc, dev);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&verrou);
    Device *D = NULL;
    for (int i = 0; i < MAX_OBJ; i++) if (!devices[i].cle) { D = &devices[i]; break; }
    if (D) {
        memset(D, 0, sizeof *D);
        D->gdpa = gdpa;
#define LIER(n) D->n = (PFN_vk##n)gdpa(*dev, "vk" #n)
        LIER(DestroyDevice); LIER(QueuePresentKHR); LIER(CmdDraw); LIER(CmdDrawIndexed); LIER(CmdDrawIndirect);
        LIER(CmdDrawIndexedIndirect); LIER(CmdDrawIndirectCount); LIER(CmdDrawIndexedIndirectCount); LIER(CmdDispatch);
        LIER(CmdBindPipeline); LIER(CmdBeginRenderPass); LIER(CmdBeginRenderPass2); LIER(CmdBeginRendering);
        LIER(CreateSwapchainKHR); LIER(CreateGraphicsPipelines); LIER(CreateComputePipelines);
#undef LIER
        Instance *I = inst_de(cle(pd));
        if (I) {
            snprintf(D->appli, sizeof D->appli, "%s", I->appli);
            if (I->GetPhysicalDeviceProperties) {
                VkPhysicalDeviceProperties p;
                I->GetPhysicalDeviceProperties(pd, &p);
                snprintf(D->carte, sizeof D->carte, "%s", p.deviceName);
            }
        }
        clock_gettime(CLOCK_MONOTONIC, &D->derniere);
        D->sortie = ouvrir_sortie();
        if (D->sortie) {
            char a[200], c[200];
            echapper(D->appli, a, sizeof a); echapper(D->carte, c, sizeof c);
            fprintf(D->sortie, "{\"ev\":\"appareil\",\"pid\":%d,\"t\":%.3f,\"appli\":\"%s\",\"carte\":\"%s\"}\n", (int)getpid(), maintenant_ms(), a, c);
            fflush(D->sortie);
        }
        D->cle = cle(*dev);          /* visible des autres fils seulement une fois rempli */
    }
    pthread_mutex_unlock(&verrou);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL pf_DestroyDevice(VkDevice dev, const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&verrou);
    Device *D = dev_de(cle(dev));
    PFN_vkDestroyDevice suivant = D ? D->DestroyDevice : NULL;
    if (D) {
        if (D->sortie) {
            fprintf(D->sortie, "{\"ev\":\"fin\",\"pid\":%d,\"t\":%.3f,\"images\":%llu,\"dessins_total\":%llu,\"pipelines_crees\":%llu}\n",
                    (int)getpid(), maintenant_ms(), (unsigned long long)D->images, (unsigned long long)D->dessins_total,
                    (unsigned long long)D->pipelines_crees);
            fclose(D->sortie);
        }
        D->cle = NULL;
    }
    pthread_mutex_unlock(&verrou);
    if (suivant) suivant(dev, alloc);
}

/* ───────────── ce que la carte reçoit ───────────── */
#define AJOUTER(champ, n) __atomic_fetch_add(&D->champ, (uint64_t)(n), __ATOMIC_RELAXED)

static VKAPI_ATTR void VKAPI_CALL pf_CmdDraw(VkCommandBuffer cb, uint32_t vc, uint32_t ic, uint32_t fv, uint32_t fi) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, 1); AJOUTER(sommets, (uint64_t)vc * ic); AJOUTER(instances, ic); AJOUTER(dessins_total, 1);
    D->CmdDraw(cb, vc, ic, fv, fi);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDrawIndexed(VkCommandBuffer cb, uint32_t ic, uint32_t inst, uint32_t fi, int32_t vo, uint32_t fin) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, 1); AJOUTER(indexes, (uint64_t)ic * inst); AJOUTER(instances, inst); AJOUTER(dessins_total, 1);
    D->CmdDrawIndexed(cb, ic, inst, fi, vo, fin);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDrawIndirect(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, uint32_t n, uint32_t s) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, n); AJOUTER(indirects, n); AJOUTER(dessins_total, n);
    D->CmdDrawIndirect(cb, b, o, n, s);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDrawIndexedIndirect(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, uint32_t n, uint32_t s) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, n); AJOUTER(indirects, n); AJOUTER(dessins_total, n);
    D->CmdDrawIndexedIndirect(cb, b, o, n, s);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDrawIndirectCount(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, VkBuffer cbuf, VkDeviceSize co, uint32_t max, uint32_t s) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, 1); AJOUTER(indirects, max); AJOUTER(dessins_total, 1);
    D->CmdDrawIndirectCount(cb, b, o, cbuf, co, max, s);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDrawIndexedIndirectCount(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, VkBuffer cbuf, VkDeviceSize co, uint32_t max, uint32_t s) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(dessins, 1); AJOUTER(indirects, max); AJOUTER(dessins_total, 1);
    D->CmdDrawIndexedIndirectCount(cb, b, o, cbuf, co, max, s);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdDispatch(VkCommandBuffer cb, uint32_t x, uint32_t y, uint32_t z) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(calculs, 1);
    D->CmdDispatch(cb, x, y, z);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdBindPipeline(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(pipelines_lies, 1);
    D->CmdBindPipeline(cb, bp, p);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdBeginRenderPass(VkCommandBuffer cb, const VkRenderPassBeginInfo *bi, VkSubpassContents c) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(passes, 1);
    D->CmdBeginRenderPass(cb, bi, c);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdBeginRenderPass2(VkCommandBuffer cb, const VkRenderPassBeginInfo *bi, const VkSubpassBeginInfo *si) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(passes, 1);
    D->CmdBeginRenderPass2(cb, bi, si);
}
static VKAPI_ATTR void VKAPI_CALL pf_CmdBeginRendering(VkCommandBuffer cb, const VkRenderingInfo *ri) {
    Device *D = dev_de(cle(cb)); if (!D) return;
    AJOUTER(passes, 1);
    D->CmdBeginRendering(cb, ri);
}

static VKAPI_ATTR VkResult VKAPI_CALL pf_CreateSwapchainKHR(VkDevice dev, const VkSwapchainCreateInfoKHR *ci, const VkAllocationCallbacks *alloc, VkSwapchainKHR *sc) {
    Device *D = dev_de(cle(dev)); if (!D) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = D->CreateSwapchainKHR(dev, ci, alloc, sc);
    if (r == VK_SUCCESS) {
        D->largeur = ci->imageExtent.width; D->hauteur = ci->imageExtent.height;
        if (D->sortie) {
            fprintf(D->sortie, "{\"ev\":\"swapchain\",\"pid\":%d,\"t\":%.3f,\"l\":%u,\"h\":%u,\"images\":%u,\"mode\":%d}\n",
                    (int)getpid(), maintenant_ms(), ci->imageExtent.width, ci->imageExtent.height, ci->minImageCount, (int)ci->presentMode);
            fflush(D->sortie);
        }
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL pf_CreateGraphicsPipelines(VkDevice dev, VkPipelineCache cache, uint32_t n, const VkGraphicsPipelineCreateInfo *ci, const VkAllocationCallbacks *alloc, VkPipeline *out) {
    Device *D = dev_de(cle(dev)); if (!D) return VK_ERROR_INITIALIZATION_FAILED;
    double t0 = maintenant_ms();
    VkResult r = D->CreateGraphicsPipelines(dev, cache, n, ci, alloc, out);
    AJOUTER(pipelines_crees, n);
    if (D->sortie) {
        fprintf(D->sortie, "{\"ev\":\"pipelines\",\"pid\":%d,\"t\":%.3f,\"genre\":\"graphique\",\"n\":%u,\"duree_ms\":%.1f,\"total\":%llu}\n",
                (int)getpid(), t0, n, maintenant_ms() - t0, (unsigned long long)D->pipelines_crees);
        fflush(D->sortie);
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL pf_CreateComputePipelines(VkDevice dev, VkPipelineCache cache, uint32_t n, const VkComputePipelineCreateInfo *ci, const VkAllocationCallbacks *alloc, VkPipeline *out) {
    Device *D = dev_de(cle(dev)); if (!D) return VK_ERROR_INITIALIZATION_FAILED;
    double t0 = maintenant_ms();
    VkResult r = D->CreateComputePipelines(dev, cache, n, ci, alloc, out);
    AJOUTER(pipelines_crees, n);
    if (D->sortie) {
        fprintf(D->sortie, "{\"ev\":\"pipelines\",\"pid\":%d,\"t\":%.3f,\"genre\":\"calcul\",\"n\":%u,\"duree_ms\":%.1f,\"total\":%llu}\n",
                (int)getpid(), t0, n, maintenant_ms() - t0, (unsigned long long)D->pipelines_crees);
        fflush(D->sortie);
    }
    return r;
}

/* une image est présentée : la ligne « Matrix » */
static VKAPI_ATTR VkResult VKAPI_CALL pf_QueuePresentKHR(VkQueue q, const VkPresentInfoKHR *pi) {
    Device *D = dev_de(cle(q)); if (!D) return VK_ERROR_DEVICE_LOST;
    VkResult r = D->QueuePresentKHR(q, pi);
    struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
    double dt = (now.tv_sec - D->derniere.tv_sec) * 1e3 + (now.tv_nsec - D->derniere.tv_nsec) / 1e6;
    D->derniere = now;
    unsigned long long n = (unsigned long long)__atomic_add_fetch(&D->images, 1, __ATOMIC_RELAXED);
#define PRENDRE(champ) (unsigned long long)__atomic_exchange_n(&D->champ, 0, __ATOMIC_RELAXED)
    unsigned long long dessins = PRENDRE(dessins), sommets = PRENDRE(sommets), instances = PRENDRE(instances),
                       indexes = PRENDRE(indexes), indirects = PRENDRE(indirects), calculs = PRENDRE(calculs),
                       pipelines = PRENDRE(pipelines_lies), passes = PRENDRE(passes);
#undef PRENDRE
    if (D->sortie) {
        fprintf(D->sortie, "{\"ev\":\"image\",\"pid\":%d,\"t\":%.3f,\"n\":%llu,\"dt_ms\":%.2f,\"dessins\":%llu,\"sommets\":%llu,"
                "\"indexes\":%llu,\"instances\":%llu,\"indirects\":%llu,\"calculs\":%llu,\"pipelines\":%llu,\"passes\":%llu,\"l\":%u,\"h\":%u}\n",
                (int)getpid(), maintenant_ms(), n, dt, dessins, sommets, indexes, instances, indirects, calculs, pipelines, passes,
                D->largeur, D->hauteur);
        fflush(D->sortie);
    }
    return r;
}

/* ───────────── énumérations (ce que la couche déclare) ───────────── */
static VKAPI_ATTR VkResult VKAPI_CALL pf_EnumerateInstanceLayerProperties(uint32_t *n, VkLayerProperties *p) {
    if (!p) { *n = 1; return VK_SUCCESS; }
    if (*n < 1) return VK_INCOMPLETE;
    *n = 1;
    memset(p, 0, sizeof *p);
    snprintf(p->layerName, sizeof p->layerName, "%s", NOM_COUCHE);
    snprintf(p->description, sizeof p->description, "JARVIS pilote-ecran : le flux de la carte, image par image (lecture seule)");
    p->implementationVersion = 1;
    p->specVersion = VK_MAKE_API_VERSION(0, 1, 3, 0);
    return VK_SUCCESS;
}
static VKAPI_ATTR VkResult VKAPI_CALL pf_EnumerateDeviceLayerProperties(VkPhysicalDevice pd, uint32_t *n, VkLayerProperties *p) {
    (void)pd; return pf_EnumerateInstanceLayerProperties(n, p);
}
static VKAPI_ATTR VkResult VKAPI_CALL pf_EnumerateInstanceExtensionProperties(const char *couche, uint32_t *n, VkExtensionProperties *p) {
    (void)p;
    if (couche && !strcmp(couche, NOM_COUCHE)) { *n = 0; return VK_SUCCESS; }
    return VK_ERROR_LAYER_NOT_PRESENT;
}
static VKAPI_ATTR VkResult VKAPI_CALL pf_EnumerateDeviceExtensionProperties(VkPhysicalDevice pd, const char *couche, uint32_t *n, VkExtensionProperties *p) {
    if (couche && !strcmp(couche, NOM_COUCHE)) { *n = 0; return VK_SUCCESS; }
    Instance *I = inst_de(cle(pd));
    if (!I || !I->EnumerateDeviceExtensionProperties) return VK_ERROR_LAYER_NOT_PRESENT;
    return I->EnumerateDeviceExtensionProperties(pd, couche, n, p);
}

/* ───────────── points d'entrée du chargeur ───────────── */
EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice dev, const char *nom);

#define SI(nom_vk, fn) if (!strcmp(nom, nom_vk)) return (PFN_vkVoidFunction)fn;
static PFN_vkVoidFunction fonctions_appareil(const char *nom) {
    SI("vkGetDeviceProcAddr", vkGetDeviceProcAddr) SI("vkDestroyDevice", pf_DestroyDevice) SI("vkQueuePresentKHR", pf_QueuePresentKHR)
    SI("vkCmdDraw", pf_CmdDraw) SI("vkCmdDrawIndexed", pf_CmdDrawIndexed) SI("vkCmdDrawIndirect", pf_CmdDrawIndirect)
    SI("vkCmdDrawIndexedIndirect", pf_CmdDrawIndexedIndirect) SI("vkCmdDrawIndirectCount", pf_CmdDrawIndirectCount)
    SI("vkCmdDrawIndirectCountKHR", pf_CmdDrawIndirectCount) SI("vkCmdDrawIndexedIndirectCount", pf_CmdDrawIndexedIndirectCount)
    SI("vkCmdDrawIndexedIndirectCountKHR", pf_CmdDrawIndexedIndirectCount) SI("vkCmdDispatch", pf_CmdDispatch)
    SI("vkCmdBindPipeline", pf_CmdBindPipeline) SI("vkCmdBeginRenderPass", pf_CmdBeginRenderPass)
    SI("vkCmdBeginRenderPass2", pf_CmdBeginRenderPass2) SI("vkCmdBeginRenderPass2KHR", pf_CmdBeginRenderPass2)
    SI("vkCmdBeginRendering", pf_CmdBeginRendering) SI("vkCmdBeginRenderingKHR", pf_CmdBeginRendering)
    SI("vkCreateSwapchainKHR", pf_CreateSwapchainKHR) SI("vkCreateGraphicsPipelines", pf_CreateGraphicsPipelines)
    SI("vkCreateComputePipelines", pf_CreateComputePipelines)
    return NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice dev, const char *nom) {
    PFN_vkVoidFunction f = fonctions_appareil(nom);
    if (f) {
        /* ne pas intercepter ce que l'appareil ne sait pas faire (extension absente) : on rend ce que dit le pilote */
        Device *D = dev ? dev_de(cle(dev)) : NULL;
        if (D && D->gdpa && !D->gdpa(dev, nom)) return NULL;
        return f;
    }
    Device *D = dev ? dev_de(cle(dev)) : NULL;
    return (D && D->gdpa) ? D->gdpa(dev, nom) : NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance inst, const char *nom) {
    SI("vkGetInstanceProcAddr", vkGetInstanceProcAddr) SI("vkCreateInstance", pf_CreateInstance) SI("vkDestroyInstance", pf_DestroyInstance)
    SI("vkCreateDevice", pf_CreateDevice) SI("vkEnumerateInstanceLayerProperties", pf_EnumerateInstanceLayerProperties)
    SI("vkEnumerateDeviceLayerProperties", pf_EnumerateDeviceLayerProperties)
    SI("vkEnumerateInstanceExtensionProperties", pf_EnumerateInstanceExtensionProperties)
    SI("vkEnumerateDeviceExtensionProperties", pf_EnumerateDeviceExtensionProperties)
    PFN_vkVoidFunction f = fonctions_appareil(nom);
    if (f) return f;
    if (!inst) return NULL;
    Instance *I = inst_de(cle(inst));
    return (I && I->gipa) ? I->gipa(inst, nom) : NULL;
}
#undef SI

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *p) {
    if (!p || p->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) return VK_ERROR_INITIALIZATION_FAILED;
    if (p->loaderLayerInterfaceVersion < 2) return VK_ERROR_INITIALIZATION_FAILED;
    p->loaderLayerInterfaceVersion = 2;
    p->pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
    p->pfnGetDeviceProcAddr = vkGetDeviceProcAddr;
    p->pfnGetPhysicalDeviceProcAddr = NULL;
    return VK_SUCCESS;
}

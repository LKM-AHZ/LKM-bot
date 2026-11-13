import mirarouterIcon from '@/assets/images/provider_logos/mirarouter.svg?no-inline';
// Provider logos are bundled from @lobehub/icons-static-svg at build time; nothing is
// fetched from jsDelivr or from providers' own sites at runtime.
import icon_ai302_color from '@lobehub/icons-static-svg/icons/ai302-color.svg?no-inline';
import icon_aihubmix_color from '@lobehub/icons-static-svg/icons/aihubmix-color.svg?no-inline';
import icon_alibabacloud_color from '@lobehub/icons-static-svg/icons/alibabacloud-color.svg?no-inline';
import icon_anthropic from '@lobehub/icons-static-svg/icons/anthropic.svg?no-inline';
import icon_azure from '@lobehub/icons-static-svg/icons/azure.svg?no-inline';
import icon_bailian_color from '@lobehub/icons-static-svg/icons/bailian-color.svg?no-inline';
import icon_coze from '@lobehub/icons-static-svg/icons/coze.svg?no-inline';
import icon_deepseek_color from '@lobehub/icons-static-svg/icons/deepseek-color.svg?no-inline';
import icon_dify_color from '@lobehub/icons-static-svg/icons/dify-color.svg?no-inline';
import icon_fastgpt_color from '@lobehub/icons-static-svg/icons/fastgpt-color.svg?no-inline';
import icon_fishaudio from '@lobehub/icons-static-svg/icons/fishaudio.svg?no-inline';
import icon_gemini_color from '@lobehub/icons-static-svg/icons/gemini-color.svg?no-inline';
import icon_groq from '@lobehub/icons-static-svg/icons/groq.svg?no-inline';
import icon_huggingface from '@lobehub/icons-static-svg/icons/huggingface.svg?no-inline';
import icon_kimi from '@lobehub/icons-static-svg/icons/kimi.svg?no-inline';
import icon_lmstudio from '@lobehub/icons-static-svg/icons/lmstudio.svg?no-inline';
import icon_longcat_color from '@lobehub/icons-static-svg/icons/longcat-color.svg?no-inline';
import icon_microsoft from '@lobehub/icons-static-svg/icons/microsoft.svg?no-inline';
import icon_minimax from '@lobehub/icons-static-svg/icons/minimax.svg?no-inline';
import icon_modelscope from '@lobehub/icons-static-svg/icons/modelscope.svg?no-inline';
import icon_nvidia_color from '@lobehub/icons-static-svg/icons/nvidia-color.svg?no-inline';
import icon_ollama from '@lobehub/icons-static-svg/icons/ollama.svg?no-inline';
import icon_openai from '@lobehub/icons-static-svg/icons/openai.svg?no-inline';
import icon_openrouter from '@lobehub/icons-static-svg/icons/openrouter.svg?no-inline';
import icon_ppio from '@lobehub/icons-static-svg/icons/ppio.svg?no-inline';
import icon_siliconcloud from '@lobehub/icons-static-svg/icons/siliconcloud.svg?no-inline';
import icon_typesafeai from '@lobehub/icons-static-svg/icons/typesafeai.svg?no-inline';
import icon_vercel from '@lobehub/icons-static-svg/icons/vercel.svg?no-inline';
import icon_vllm from '@lobehub/icons-static-svg/icons/vllm.svg?no-inline';
import icon_volcengine_color from '@lobehub/icons-static-svg/icons/volcengine-color.svg?no-inline';
import icon_xai from '@lobehub/icons-static-svg/icons/xai.svg?no-inline';
import icon_xiaomimimo from '@lobehub/icons-static-svg/icons/xiaomimimo.svg?no-inline';
import icon_xinference_color from '@lobehub/icons-static-svg/icons/xinference-color.svg?no-inline';
import icon_zhipu from '@lobehub/icons-static-svg/icons/zhipu.svg?no-inline';

/**
 * 提供商相关的工具函数
 */

/**
 * 获取提供商类型对应的图标
 * @param {string} type - 提供商类型
 * @returns {string} 图标 URL
 */
export function getProviderIcon(type) {
  const icons = {
    'openai': icon_openai,
    'azure': icon_azure,
    'xai': icon_xai,
    'anthropic': icon_anthropic,
    'ollama': icon_ollama,
    'google': icon_gemini_color,
    'deepseek': icon_deepseek_color,
    'modelscope': icon_modelscope,
    'zhipu': icon_zhipu,
    'nvidia': icon_nvidia_color,
    'siliconflow': icon_siliconcloud,
    'moonshot': icon_kimi,
    'kimi': icon_kimi,
    'kimi-code': icon_kimi,
    'longcat': icon_longcat_color,
    'ppio': icon_ppio,
    'dify': icon_dify_color,
    'coze': icon_coze,
    'dashscope': icon_alibabacloud_color,
    'fastgpt': icon_fastgpt_color,
    'lm_studio': icon_lmstudio,
    'fishaudio': icon_fishaudio,
    'minimax': icon_minimax,
    'minimax-token-plan': icon_minimax,
    'mimo': icon_xiaomimimo,
    'xiaomi': icon_xiaomimimo,
    'xiaomi-token-plan': icon_xiaomimimo,
    '302ai': icon_ai302_color,
    'microsoft': icon_microsoft,
    'vllm': icon_vllm,
    'groq': icon_groq,
    'aihubmix': icon_aihubmix_color,
    'openrouter': icon_openrouter,
    'typesafe': icon_typesafeai,
    'vercel': icon_vercel,
    'xinference': icon_xinference_color,
    'bailian': icon_bailian_color,
    'volcengine': icon_volcengine_color,
    'huggingface': icon_huggingface,
  };
  return icons[type] || '';
}

/**
 * Determine whether a provider icon is a monochrome SVG.
 *
 * These icons need to be inverted in the dark theme because they are loaded as
 * external images and cannot inherit the page text color.
 *
 * @param {string} type - Provider type
 * @returns {boolean} Whether the icon should be theme-inverted
 */
export function isMonochromeProviderIcon(type) {
  return [
    'openai',
    'azure',
    'xai',
    'anthropic',
    'ollama',
    'modelscope',
    'zhipu',
    'siliconflow',
    'moonshot',
    'kimi',
    'kimi-code',
    'ppio',
    'lm_studio',
    'minimax',
    'minimax-token-plan',
    'mimo',
    'xiaomi',
    'xiaomi-token-plan',
    'openrouter',
    'typesafe',
    'vercel',
    'groq',
    'microsoft',
    'fishaudio',
    'vllm',
    'huggingface',
    'coze',
    'deerflow'
  ].includes(type);
}

/**
 * 获取提供商简介
 * @param {Object} template - 模板对象
 * @param {string} name - 提供商名称
 * @param {Function} tm - 翻译函数
 * @returns {string} 提供商描述
 */
export function getProviderDescription(template, name, tm) {
  if (name === 'OpenAI') {
    return tm('providers.description.openai', { type: template.type });
  } else if (template.provider === 'kimi-code') {
    return tm('providers.description.kimi_code');
  } else if (name === 'vLLM Rerank') {
    return tm('providers.description.vllm_rerank', { type: template.type });
  }
  return tm('providers.description.default', { type: template.type });
}

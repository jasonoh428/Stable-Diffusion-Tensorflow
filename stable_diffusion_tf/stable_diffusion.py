import tensorflow as tf
from tensorflow import keras
import numpy as np
from tqdm import tqdm
import math

from .autoencoder_kl import Decoder
from .diffusion_model import UNetModel
from .clip_encoder import CLIPTextTransformer
from .clip_tokeniser import SimpleTokenizer

MAX_TEXT_LEN = 77


class Text2Image:
    def __init__(
        self, img_height=1000, img_width=1000, batch_size=1, jit_compile=False
    ):
        self.img_height = img_height
        self.img_width = img_width
        self.batch_size = batch_size
        self.tokenizer = SimpleTokenizer()

        text_encoder, diffusion_model, decoder = get_model(img_height, img_width)
        self.text_encoder = text_encoder
        self.diffusion_model = diffusion_model
        self.decoder = decoder
        if jit_compile:
            self.text_encoder.compile(jit_compile=True)
            self.diffusion_model.compile(jit_compile=True)
            self.decoder.compile(jit_compile=True)

        tokens_unconditional = np.array(_TOKENS_UNCONDITIONAL)[None].astype("int32")
        tokens_unconditional = np.repeat(tokens_unconditional, batch_size, axis=0)
        self.tokens_unconditional = tf.convert_to_tensor(tokens_unconditional)

    def timestep_embedding(self, timesteps, dim=320, max_period=10000):
        half = dim // 2
        freqs = np.exp(
            -math.log(max_period) * np.arange(0, half, dtype="float32") / half
        )
        args = np.array(timesteps) * freqs
        embedding = np.concatenate([np.cos(args), np.sin(args)])
        return tf.convert_to_tensor(embedding.reshape(1, -1))

    def get_model_output(
        self, latent, t, context, unconditional_context, unconditional_guidance_scale
    ):
        timesteps = np.array([t])
        t_emb = self.timestep_embedding(timesteps)
        t_emb = np.repeat(t_emb, self.batch_size, axis=0)
        unconditional_latent = self.diffusion_model.predict_on_batch(
            [latent, t_emb, unconditional_context]
        )
        latent = self.diffusion_model.predict_on_batch([latent, t_emb, context])
        return unconditional_latent + unconditional_guidance_scale * (
            latent - unconditional_latent
        )

    def get_x_prev_and_pred_x0(self, x, e_t, index, a_t, a_prev, temperature):
        sigma_t = 0
        sqrt_one_minus_at = math.sqrt(1 - a_t)
        pred_x0 = (x - sqrt_one_minus_at * e_t) / math.sqrt(a_t)

        # Direction pointing to x_t
        dir_xt = math.sqrt(1.0 - a_prev - sigma_t**2) * e_t
        noise = sigma_t * tf.random.normal(x.shape) * temperature
        x_prev = math.sqrt(a_prev) * pred_x0 + dir_xt
        return x_prev, pred_x0

    def generate(
        self, prompt, n_steps=25, unconditional_guidance_scale=7.5, temperature=1
    ):
        n_h = self.img_height // 8
        n_w = self.img_width // 8

        inputs = self.tokenizer.encode(prompt)
        assert len(inputs) < 77, "Prompt is too long (should be < 77 tokens)"
        phrase = inputs + [49407] * (77 - len(inputs))

        pos_ids = tf.convert_to_tensor(np.array(list(range(77)))[None].astype("int32"))
        pos_ids = np.repeat(pos_ids, self.batch_size, axis=0)

        # Get context
        phrase = np.array(phrase)[None].astype("int32")
        phrase = np.repeat(phrase, self.batch_size, axis=0)
        phrase = tf.convert_to_tensor(phrase)
        context = self.text_encoder.predict_on_batch([phrase, pos_ids])

        unconditional_context = self.text_encoder.predict_on_batch(
            [self.tokens_unconditional, pos_ids]
        )

        timesteps = list(np.arange(1, 1000, 1000 // n_steps))
        print(f"Running for {timesteps} timesteps")

        alphas = [_ALPHAS_CUMPROD[t] for t in timesteps]
        alphas_prev = [1.0] + alphas[:-1]

        latent = tf.random.normal((self.batch_size, n_h, n_w, 4))

        t = tqdm(list(enumerate(timesteps))[::-1])
        for index, timestep in t:
            t.set_description("%3d %3d" % (index, timestep))
            e_t = self.get_model_output(
                latent,
                timestep,
                context,
                unconditional_context,
                unconditional_guidance_scale,
            )
            a_t, a_prev = alphas[index], alphas_prev[index]
            x_prev, pred_x0 = self.get_x_prev_and_pred_x0(
                latent, e_t, index, a_t, a_prev, temperature
            )
            latent = x_prev

        decoded = self.decoder.predict_on_batch(latent)
        decoded = ((decoded + 1) / 2) * 255
        return np.clip(decoded, 0, 255).astype("uint8")


def get_model(img_height, img_width, download_weights=True):
    MAX_TEXT_LEN = 77
    n_h = img_height // 8
    n_w = img_width // 8

    input_word_ids = keras.layers.Input(shape=(MAX_TEXT_LEN,), dtype=tf.int32)
    input_pos_ids = keras.layers.Input(shape=(MAX_TEXT_LEN,), dtype=tf.int32)
    embeds = CLIPTextTransformer()([input_word_ids, input_pos_ids])
    text_encoder = keras.models.Model([input_word_ids, input_pos_ids], embeds)

    context = keras.layers.Input((MAX_TEXT_LEN, 768))
    t_emb = keras.layers.Input((320,))
    latent = keras.layers.Input((n_h, n_w, 4))
    unet = UNetModel()

    diffusion_model = keras.models.Model(
        [latent, t_emb, context], unet([latent, t_emb, context])
    )

    latent = keras.layers.Input((n_h, n_w, 4))
    decoder = Decoder()
    decoder = keras.models.Model(latent, decoder(latent))

    text_encoder.load_weights("text_encoder.h5")
    diffusion_model.load_weights("diffusion_model.h5")
    decoder.load_weights("decoder.h5")

    return text_encoder, diffusion_model, decoder


_TOKENS_UNCONDITIONAL = [
    49406,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
    49407,
]
_ALPHAS_CUMPROD = [
    0.99915,
    0.998296,
    0.9974381,
    0.9965762,
    0.99571025,
    0.9948404,
    0.9939665,
    0.9930887,
    0.9922069,
    0.9913211,
    0.9904313,
    0.98953754,
    0.9886398,
    0.9877381,
    0.9868324,
    0.98592263,
    0.98500896,
    0.9840913,
    0.9831696,
    0.982244,
    0.98131436,
    0.9803808,
    0.97944313,
    0.97850156,
    0.977556,
    0.9766064,
    0.97565293,
    0.9746954,
    0.9737339,
    0.9727684,
    0.97179896,
    0.97082555,
    0.96984816,
    0.96886677,
    0.9678814,
    0.96689206,
    0.96589875,
    0.9649015,
    0.96390027,
    0.9628951,
    0.9618859,
    0.96087277,
    0.95985574,
    0.95883465,
    0.9578097,
    0.95678073,
    0.95574784,
    0.954711,
    0.95367026,
    0.9526256,
    0.9515769,
    0.95052433,
    0.94946784,
    0.94840735,
    0.947343,
    0.94627476,
    0.9452025,
    0.9441264,
    0.9430464,
    0.9419625,
    0.9408747,
    0.939783,
    0.9386874,
    0.93758786,
    0.9364845,
    0.93537724,
    0.9342661,
    0.9331511,
    0.9320323,
    0.9309096,
    0.929783,
    0.9286526,
    0.9275183,
    0.9263802,
    0.92523825,
    0.92409253,
    0.92294294,
    0.9217895,
    0.92063236,
    0.9194713,
    0.9183065,
    0.9171379,
    0.91596556,
    0.9147894,
    0.9136095,
    0.91242576,
    0.9112383,
    0.9100471,
    0.9088522,
    0.9076535,
    0.9064511,
    0.90524495,
    0.9040351,
    0.90282154,
    0.9016043,
    0.90038335,
    0.8991587,
    0.8979304,
    0.8966984,
    0.89546275,
    0.89422345,
    0.8929805,
    0.89173394,
    0.89048374,
    0.88922995,
    0.8879725,
    0.8867115,
    0.88544685,
    0.88417864,
    0.88290685,
    0.8816315,
    0.88035256,
    0.8790701,
    0.87778413,
    0.8764946,
    0.8752016,
    0.873905,
    0.87260497,
    0.8713014,
    0.8699944,
    0.86868393,
    0.86737,
    0.8660526,
    0.8647318,
    0.86340755,
    0.8620799,
    0.8607488,
    0.85941434,
    0.8580765,
    0.8567353,
    0.8553907,
    0.8540428,
    0.85269153,
    0.85133696,
    0.84997904,
    0.84861785,
    0.8472533,
    0.8458856,
    0.8445145,
    0.84314024,
    0.84176266,
    0.8403819,
    0.8389979,
    0.8376107,
    0.8362203,
    0.83482677,
    0.83343,
    0.8320301,
    0.8306271,
    0.8292209,
    0.82781166,
    0.82639927,
    0.8249838,
    0.82356524,
    0.8221436,
    0.82071894,
    0.81929123,
    0.81786054,
    0.8164268,
    0.8149901,
    0.8135504,
    0.81210774,
    0.81066215,
    0.8092136,
    0.8077621,
    0.80630773,
    0.80485046,
    0.8033903,
    0.80192727,
    0.8004614,
    0.79899275,
    0.79752123,
    0.7960469,
    0.7945698,
    0.7930899,
    0.79160726,
    0.7901219,
    0.7886338,
    0.787143,
    0.7856495,
    0.7841533,
    0.78265446,
    0.78115296,
    0.7796488,
    0.77814204,
    0.7766327,
    0.7751208,
    0.7736063,
    0.77208924,
    0.7705697,
    0.7690476,
    0.767523,
    0.7659959,
    0.7644664,
    0.76293445,
    0.7614,
    0.7598632,
    0.75832397,
    0.75678235,
    0.75523835,
    0.75369203,
    0.7521434,
    0.75059247,
    0.7490392,
    0.7474837,
    0.7459259,
    0.7443659,
    0.74280363,
    0.7412392,
    0.7396726,
    0.7381038,
    0.73653287,
    0.7349598,
    0.7333846,
    0.73180735,
    0.730228,
    0.7286466,
    0.7270631,
    0.7254777,
    0.72389024,
    0.72230077,
    0.7207094,
    0.71911603,
    0.7175208,
    0.7159236,
    0.71432453,
    0.7127236,
    0.71112084,
    0.7095162,
    0.7079098,
    0.7063016,
    0.70469165,
    0.70307994,
    0.7014665,
    0.69985133,
    0.6982345,
    0.696616,
    0.6949958,
    0.69337404,
    0.69175065,
    0.69012564,
    0.6884991,
    0.68687093,
    0.6852413,
    0.68361014,
    0.6819775,
    0.6803434,
    0.67870784,
    0.6770708,
    0.6754324,
    0.6737926,
    0.67215145,
    0.670509,
    0.66886514,
    0.66722,
    0.6655736,
    0.66392595,
    0.662277,
    0.6606269,
    0.65897554,
    0.657323,
    0.65566933,
    0.6540145,
    0.6523586,
    0.6507016,
    0.6490435,
    0.64738435,
    0.6457241,
    0.64406294,
    0.6424008,
    0.64073765,
    0.63907355,
    0.63740855,
    0.6357426,
    0.6340758,
    0.6324082,
    0.6307397,
    0.6290704,
    0.6274003,
    0.6257294,
    0.62405777,
    0.6223854,
    0.62071234,
    0.6190386,
    0.61736417,
    0.6156891,
    0.61401343,
    0.6123372,
    0.6106603,
    0.6089829,
    0.607305,
    0.6056265,
    0.6039476,
    0.60226816,
    0.6005883,
    0.598908,
    0.59722733,
    0.5955463,
    0.59386486,
    0.5921831,
    0.59050107,
    0.5888187,
    0.5871361,
    0.5854532,
    0.5837701,
    0.5820868,
    0.5804033,
    0.5787197,
    0.5770359,
    0.575352,
    0.57366806,
    0.571984,
    0.5702999,
    0.5686158,
    0.56693166,
    0.56524754,
    0.5635635,
    0.5618795,
    0.56019557,
    0.5585118,
    0.5568281,
    0.55514455,
    0.5534612,
    0.551778,
    0.5500951,
    0.5484124,
    0.54673,
    0.5450478,
    0.54336596,
    0.54168445,
    0.54000324,
    0.53832245,
    0.5366421,
    0.53496206,
    0.5332825,
    0.53160346,
    0.5299248,
    0.52824676,
    0.5265692,
    0.52489215,
    0.5232157,
    0.5215398,
    0.51986456,
    0.51818997,
    0.51651603,
    0.51484275,
    0.5131702,
    0.5114983,
    0.5098272,
    0.50815684,
    0.5064873,
    0.50481856,
    0.50315064,
    0.50148356,
    0.4998174,
    0.4981521,
    0.49648774,
    0.49482432,
    0.49316183,
    0.49150035,
    0.48983985,
    0.4881804,
    0.486522,
    0.48486462,
    0.4832084,
    0.48155323,
    0.4798992,
    0.47824633,
    0.47659463,
    0.4749441,
    0.47329482,
    0.4716468,
    0.47,
    0.46835446,
    0.46671024,
    0.46506736,
    0.4634258,
    0.46178558,
    0.46014675,
    0.45850933,
    0.45687333,
    0.45523876,
    0.45360568,
    0.45197406,
    0.45034397,
    0.44871536,
    0.44708833,
    0.44546285,
    0.44383895,
    0.44221666,
    0.440596,
    0.43897697,
    0.43735963,
    0.43574396,
    0.43412998,
    0.43251774,
    0.43090722,
    0.4292985,
    0.42769152,
    0.42608637,
    0.42448303,
    0.4228815,
    0.42128187,
    0.4196841,
    0.41808826,
    0.4164943,
    0.4149023,
    0.41331223,
    0.41172415,
    0.41013804,
    0.40855396,
    0.4069719,
    0.4053919,
    0.40381396,
    0.4022381,
    0.40066436,
    0.39909273,
    0.39752322,
    0.3959559,
    0.39439073,
    0.39282778,
    0.39126703,
    0.3897085,
    0.3881522,
    0.3865982,
    0.38504648,
    0.38349706,
    0.38194993,
    0.38040516,
    0.37886274,
    0.37732267,
    0.375785,
    0.37424973,
    0.37271687,
    0.37118647,
    0.36965853,
    0.36813304,
    0.36661002,
    0.36508954,
    0.36357155,
    0.3620561,
    0.36054322,
    0.3590329,
    0.35752517,
    0.35602003,
    0.35451752,
    0.35301763,
    0.3515204,
    0.3500258,
    0.3485339,
    0.3470447,
    0.34555823,
    0.34407446,
    0.34259343,
    0.34111515,
    0.33963963,
    0.33816692,
    0.336697,
    0.3352299,
    0.33376563,
    0.3323042,
    0.33084565,
    0.32938993,
    0.32793713,
    0.3264872,
    0.32504022,
    0.32359615,
    0.32215503,
    0.32071686,
    0.31928164,
    0.31784943,
    0.3164202,
    0.314994,
    0.3135708,
    0.31215066,
    0.31073356,
    0.3093195,
    0.30790854,
    0.30650064,
    0.30509588,
    0.30369422,
    0.30229566,
    0.30090025,
    0.299508,
    0.2981189,
    0.29673296,
    0.29535022,
    0.2939707,
    0.29259437,
    0.29122123,
    0.28985137,
    0.28848472,
    0.28712133,
    0.2857612,
    0.28440437,
    0.2830508,
    0.28170055,
    0.2803536,
    0.27900997,
    0.27766964,
    0.27633268,
    0.27499905,
    0.2736688,
    0.27234194,
    0.27101842,
    0.2696983,
    0.26838157,
    0.26706827,
    0.26575837,
    0.26445192,
    0.26314887,
    0.2618493,
    0.26055318,
    0.2592605,
    0.25797132,
    0.2566856,
    0.2554034,
    0.25412467,
    0.25284946,
    0.25157773,
    0.2503096,
    0.24904492,
    0.24778382,
    0.24652626,
    0.24527225,
    0.2440218,
    0.24277493,
    0.24153163,
    0.24029191,
    0.23905578,
    0.23782326,
    0.23659433,
    0.23536903,
    0.23414734,
    0.23292927,
    0.23171483,
    0.23050404,
    0.22929688,
    0.22809339,
    0.22689353,
    0.22569734,
    0.22450483,
    0.22331597,
    0.2221308,
    0.22094932,
    0.21977153,
    0.21859743,
    0.21742703,
    0.21626033,
    0.21509734,
    0.21393807,
    0.21278252,
    0.21163069,
    0.21048258,
    0.20933822,
    0.20819758,
    0.2070607,
    0.20592754,
    0.20479813,
    0.20367248,
    0.20255059,
    0.20143245,
    0.20031808,
    0.19920748,
    0.19810064,
    0.19699757,
    0.19589828,
    0.19480278,
    0.19371104,
    0.1926231,
    0.19153893,
    0.19045855,
    0.18938197,
    0.18830918,
    0.18724018,
    0.18617497,
    0.18511358,
    0.18405597,
    0.18300217,
    0.18195218,
    0.18090598,
    0.1798636,
    0.17882504,
    0.17779027,
    0.1767593,
    0.17573217,
    0.17470883,
    0.1736893,
    0.1726736,
    0.1716617,
    0.17065361,
    0.16964935,
    0.1686489,
    0.16765225,
    0.16665943,
    0.16567042,
    0.16468522,
    0.16370384,
    0.16272627,
    0.16175252,
    0.16078258,
    0.15981644,
    0.15885411,
    0.1578956,
    0.15694089,
    0.15599,
    0.15504292,
    0.15409963,
    0.15316014,
    0.15222447,
    0.15129258,
    0.1503645,
    0.14944021,
    0.14851972,
    0.14760303,
    0.14669013,
    0.14578101,
    0.14487568,
    0.14397413,
    0.14307636,
    0.14218238,
    0.14129217,
    0.14040573,
    0.13952307,
    0.13864417,
    0.13776903,
    0.13689767,
    0.13603005,
    0.13516618,
    0.13430607,
    0.13344972,
    0.1325971,
    0.13174823,
    0.1309031,
    0.13006169,
    0.12922402,
    0.12839006,
    0.12755983,
    0.12673332,
    0.12591052,
    0.12509143,
    0.12427604,
    0.12346435,
    0.12265636,
    0.121852055,
    0.12105144,
    0.1202545,
    0.11946124,
    0.11867165,
    0.11788572,
    0.11710346,
    0.11632485,
    0.115549885,
    0.11477857,
    0.11401089,
    0.11324684,
    0.11248643,
    0.11172963,
    0.11097645,
    0.110226884,
    0.10948092,
    0.10873855,
    0.10799977,
    0.107264586,
    0.106532976,
    0.105804935,
    0.10508047,
    0.10435956,
    0.1036422,
    0.10292839,
    0.10221813,
    0.1015114,
    0.10080819,
    0.100108504,
    0.09941233,
    0.098719664,
    0.0980305,
    0.09734483,
    0.09666264,
    0.09598393,
    0.095308684,
    0.09463691,
    0.093968585,
    0.09330372,
    0.092642285,
    0.09198428,
    0.09132971,
    0.09067855,
    0.090030804,
    0.089386456,
    0.088745505,
    0.088107936,
    0.08747375,
    0.08684293,
    0.08621547,
    0.085591376,
    0.084970616,
    0.08435319,
    0.0837391,
    0.08312833,
    0.08252087,
    0.08191671,
    0.08131585,
    0.08071827,
    0.080123976,
    0.07953294,
    0.078945175,
    0.078360654,
    0.077779375,
    0.07720133,
    0.07662651,
    0.07605491,
    0.07548651,
    0.07492131,
    0.0743593,
    0.07380046,
    0.073244795,
    0.07269229,
    0.07214294,
    0.07159673,
    0.07105365,
    0.070513695,
    0.06997685,
    0.069443114,
    0.06891247,
    0.06838491,
    0.067860425,
    0.06733901,
    0.066820644,
    0.06630533,
    0.06579305,
    0.0652838,
    0.06477757,
    0.06427433,
    0.0637741,
    0.063276865,
    0.06278259,
    0.062291294,
    0.061802953,
    0.06131756,
    0.0608351,
    0.060355574,
    0.05987896,
    0.059405252,
    0.058934443,
    0.05846652,
    0.058001474,
    0.057539295,
    0.05707997,
    0.056623492,
    0.05616985,
    0.05571903,
    0.055271026,
    0.054825824,
    0.05438342,
    0.053943794,
    0.053506944,
    0.05307286,
    0.052641522,
    0.052212927,
    0.051787063,
    0.051363923,
    0.05094349,
    0.050525755,
    0.05011071,
    0.04969834,
    0.049288645,
    0.0488816,
    0.048477206,
    0.048075445,
    0.04767631,
    0.047279786,
    0.04688587,
    0.046494544,
    0.046105802,
    0.04571963,
    0.04533602,
    0.04495496,
    0.04457644,
    0.044200446,
    0.04382697,
    0.043456003,
    0.043087535,
    0.042721547,
    0.042358037,
    0.04199699,
    0.041638397,
    0.041282244,
    0.040928524,
    0.040577225,
    0.040228333,
    0.039881844,
    0.039537743,
    0.039196018,
    0.038856663,
    0.038519662,
    0.038185004,
    0.037852682,
    0.037522685,
    0.037195,
    0.036869615,
    0.036546525,
    0.036225714,
    0.03590717,
    0.035590887,
    0.035276853,
    0.034965057,
    0.034655485,
    0.03434813,
    0.03404298,
    0.033740025,
    0.033439253,
    0.033140652,
    0.032844216,
    0.03254993,
    0.032257784,
    0.03196777,
    0.031679876,
    0.031394087,
    0.031110398,
    0.030828796,
    0.030549273,
    0.030271813,
    0.02999641,
    0.029723052,
    0.029451728,
    0.029182427,
    0.02891514,
    0.028649855,
    0.028386563,
    0.028125253,
    0.02786591,
    0.027608532,
    0.027353102,
    0.027099613,
    0.026848052,
    0.026598409,
    0.026350675,
    0.02610484,
    0.02586089,
    0.02561882,
    0.025378617,
    0.025140269,
    0.024903767,
    0.0246691,
    0.02443626,
    0.024205236,
    0.023976017,
    0.023748592,
    0.023522953,
    0.023299087,
    0.023076987,
    0.022856642,
    0.02263804,
    0.022421172,
    0.022206029,
    0.0219926,
    0.021780876,
    0.021570845,
    0.021362498,
    0.021155827,
    0.020950818,
    0.020747466,
    0.020545758,
    0.020345684,
    0.020147236,
    0.019950403,
    0.019755175,
    0.019561544,
    0.019369498,
    0.019179028,
    0.018990126,
    0.01880278,
    0.018616982,
    0.018432721,
    0.01824999,
    0.018068777,
    0.017889075,
    0.017710872,
    0.01753416,
    0.017358929,
    0.017185168,
    0.017012872,
    0.016842028,
    0.016672628,
    0.016504662,
    0.016338123,
    0.016173,
    0.016009282,
    0.015846964,
    0.015686033,
    0.015526483,
    0.015368304,
    0.015211486,
    0.0150560215,
    0.014901901,
    0.014749114,
    0.014597654,
    0.014447511,
    0.0142986765,
    0.014151142,
    0.014004898,
    0.013859936,
    0.013716248,
    0.0135738235,
    0.013432656,
    0.013292736,
    0.013154055,
    0.013016605,
    0.012880377,
    0.012745362,
    0.012611552,
    0.012478939,
    0.012347515,
    0.01221727,
    0.012088198,
    0.0119602885,
    0.0118335355,
    0.011707929,
    0.011583461,
    0.011460125,
    0.011337912,
    0.011216813,
    0.011096821,
    0.010977928,
    0.0108601255,
    0.010743406,
    0.010627762,
    0.0105131855,
    0.010399668,
    0.010287202,
    0.01017578,
    0.010065395,
    0.009956039,
    0.009847702,
    0.009740381,
    0.0096340645,
    0.009528747,
    0.009424419,
    0.009321076,
    0.009218709,
    0.00911731,
    0.009016872,
    0.008917389,
    0.008818853,
    0.008721256,
    0.008624591,
    0.008528852,
    0.00843403,
    0.00834012,
    0.008247114,
    0.008155004,
    0.008063785,
    0.007973449,
    0.007883989,
    0.007795398,
    0.0077076694,
    0.0076207966,
    0.0075347726,
    0.007449591,
    0.0073652444,
    0.007281727,
    0.0071990318,
    0.007117152,
    0.0070360815,
    0.0069558136,
    0.0068763415,
    0.006797659,
    0.00671976,
    0.0066426382,
    0.0065662866,
    0.006490699,
    0.0064158696,
    0.006341792,
    0.00626846,
    0.0061958674,
    0.0061240084,
    0.0060528764,
    0.0059824656,
    0.0059127696,
    0.0058437833,
    0.0057755,
    0.0057079145,
    0.00564102,
    0.0055748112,
    0.0055092825,
    0.005444428,
    0.005380241,
    0.0053167176,
    0.005253851,
    0.005191636,
    0.005130066,
    0.0050691366,
    0.0050088423,
    0.0049491767,
    0.004890135,
    0.0048317118,
    0.004773902,
    0.004716699,
    0.0046600983,
]

"""Tests de la logica pura: resolucion de URLs y seleccion de TV.

Correr con:  python3 -m unittest discover -s tests
(usando el interprete del venv, que tiene upnpclient)
"""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import discovery
from stream2tv.cli import resolver_url


def tv_falsa(nombre, ip, udn=None):
    return discovery.TV(
        udn=udn or f"uuid:{nombre}",
        nombre=nombre,
        location=f"http://{ip}:1700/desc.xml",
        device=None,
    )


class TestResolverURL(unittest.TestCase):
    def test_canal_pelado_usa_la_plataforma_del_comando(self):
        self.assertEqual(resolver_url("xqc", "twitch"), "https://twitch.tv/xqc")
        self.assertEqual(resolver_url("lacobraaa", "kick"), "https://kick.com/lacobraaa")

    def test_prefijo_explicito_gana_sobre_la_plataforma(self):
        self.assertEqual(resolver_url("tw:xqc", "kick"), "https://twitch.tv/xqc")
        self.assertEqual(resolver_url("ki:algo", "twitch"), "https://kick.com/algo")

    def test_url_completa_pasa_intacta(self):
        url = "https://youtube.com/watch?v=abc"
        self.assertEqual(resolver_url(url, "twitch"), url)

    def test_sin_plataforma_asume_kick(self):
        self.assertEqual(resolver_url("algo", None), "https://kick.com/algo")

    def test_recorta_espacios(self):
        self.assertEqual(resolver_url("  xqc  ", "twitch"), "https://twitch.tv/xqc")


class TestCoincidencia(unittest.TestCase):
    def setUp(self):
        self.tv = tv_falsa("[LG] webOS TV UP7710PSB", "192.168.0.50")

    def test_por_nombre_parcial_sin_importar_mayusculas(self):
        self.assertTrue(self.tv.coincide("webos"))
        self.assertTrue(self.tv.coincide("UP7710"))

    def test_por_ip_exacta(self):
        self.assertTrue(self.tv.coincide("192.168.0.50"))

    def test_no_coincide(self):
        self.assertFalse(self.tv.coincide("samsung"))

    def test_host_sale_de_la_location(self):
        self.assertEqual(self.tv.host, "192.168.0.50")


class TestElegir(unittest.TestCase):
    def setUp(self):
        self.lg = tv_falsa("[LG] webOS TV", "192.168.0.50", udn="uuid:lg")
        self.samsung = tv_falsa("Samsung 7 Series", "192.168.0.60", udn="uuid:sam")

    def test_sin_tvs_sale_con_error(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.elegir([])

    def test_una_sola_se_elige_sola(self):
        self.assertIs(discovery.elegir([self.lg]), self.lg)

    def test_filtro_selecciona(self):
        elegida = discovery.elegir([self.lg, self.samsung], filtro="samsung")
        self.assertIs(elegida, self.samsung)

    def test_filtro_ambiguo_falla_en_vez_de_adivinar(self):
        otra_lg = tv_falsa("[LG] otra tele", "192.168.0.70", udn="uuid:lg2")
        with mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.elegir([self.lg, otra_lg], filtro="lg")

    def test_filtro_sin_resultados_falla(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.elegir([self.lg], filtro="sony")

    def test_udn_preferido_gana_cuando_hay_varias(self):
        elegida = discovery.elegir([self.lg, self.samsung], udn_preferido="uuid:sam")
        self.assertIs(elegida, self.samsung)

    def test_filtro_le_gana_al_udn_preferido(self):
        elegida = discovery.elegir([self.lg, self.samsung], filtro="lg", udn_preferido="uuid:sam")
        self.assertIs(elegida, self.lg)

    def test_udn_preferido_ausente_cae_al_menu(self):
        # La TV guardada no esta en la red y no hay terminal: debe pedir que se elija.
        with mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.elegir(
                    [self.lg, self.samsung], udn_preferido="uuid:que-no-esta", interactivo=False
                )

    def test_varias_sin_terminal_no_adivina(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.elegir([self.lg, self.samsung], interactivo=False)

    def test_menu_interactivo_devuelve_la_opcion_tipeada(self):
        with mock.patch("builtins.input", return_value="2"), mock.patch("sys.stdout", new=io.StringIO()):
            elegida = discovery.elegir([self.lg, self.samsung], interactivo=True)
        self.assertIs(elegida, self.samsung)

    def test_menu_reintenta_si_la_opcion_es_invalida(self):
        with mock.patch("builtins.input", side_effect=["9", "abc", "1"]), \
             mock.patch("sys.stdout", new=io.StringIO()):
            elegida = discovery.elegir([self.lg, self.samsung], interactivo=True)
        self.assertIs(elegida, self.lg)

    def test_deduplica_por_udn(self):
        # Una TV que se anuncia dos veces (dos interfaces) no debe aparecer duplicada.
        gemela = tv_falsa("[LG] webOS TV", "192.168.0.50", udn="uuid:lg")
        with mock.patch.object(discovery, "_buscar_locations", return_value={"a", "b"}), \
             mock.patch.object(discovery, "_construir", side_effect=[self.lg, gemela]):
            self.assertEqual(len(discovery.descubrir()), 1)


if __name__ == "__main__":
    unittest.main()
